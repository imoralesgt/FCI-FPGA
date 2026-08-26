#include "fci_core.hpp"

// Widen-then-negate avoids the classic two's complement corner case where the
// most negative fft_sample_t (-1.0) cannot be negated within its own type.
static inline bin_mag_t fixed_abs(fft_sample_t x) {
  ap_fixed<17, 2> wide = x;
  if (wide < 0)
    wide = -wide;
  return bin_mag_t(wide);
}

static void axis_to_fft(hls::stream<axis_in_t> &s_axis_data, hls::stream<fft_cplx_t> &xn_s,
                         hls::stream<hls::ip_fft::config_t<fci_fft_config> > &config_s,
                         hls::stream<ap_uint<64> > &tag_s) {
  hls::ip_fft::config_t<fci_fft_config> cfg;
  cfg.setDir(1); // forward transform
  config_s.write(cfg);

  ap_uint<64> tag = 0;
SAMPLE_LOOP:
  for (unsigned i = 0; i < N_SAMPLES; i++) {
#pragma HLS pipeline II = 1
    axis_in_t beat = s_axis_data.read();
    // TUSER is held constant by trigger_core for every beat of one capture, so it only needs
    // capturing once; reading it here rather than after the loop keeps the loop body identical
    // for every iteration, which is what II=1 pipelining wants.
    if (i == 0)
      tag = beat.user;
    ap_uint<ADC_WIDTH> code = beat.data(ADC_WIDTH - 1, 0);
    // Unsigned ADC code -> signed, centered on mid-scale.
    ap_int<ADC_WIDTH> centered = ap_int<ADC_WIDTH>(code) - ap_int<ADC_WIDTH>(1 << (ADC_WIDTH - 1));
    // Left-justify the signed code into the top ADC_WIDTH bits of the 16-bit fixed-point word:
    // represented value = centered / 2^(ADC_WIDTH-1), i.e. a normalized fraction in [-1,1).
    ap_uint<16> raw = 0;
    raw.range(15, 16 - ADC_WIDTH) = centered;
    fft_sample_t sample;
    sample.range(15, 0) = raw;
    xn_s.write(fft_cplx_t(sample, fft_sample_t(0)));
  }
  // Written once per frame, after the loop rather than conditionally inside it: the FFT IP
  // (hls::fft) is a fixed-format black box between xn_s and xk_s and cannot carry a sideband tag
  // through its own stream, so the tag bypasses it entirely on this parallel stream instead.
  tag_s.write(tag);
}

static void fft_to_psa(hls::stream<fft_cplx_t> &xk_s,
                        hls::stream<hls::ip_fft::status_t<fci_fft_config> > &status_s,
                        ap_uint<BIN_INDEX_WIDTH> psa_l_lo, ap_uint<BIN_INDEX_WIDTH> psa_l_hi,
                        ap_uint<BIN_INDEX_WIDTH> psa_w_lo, ap_uint<BIN_INDEX_WIDTH> psa_w_hi,
                        hls::stream<axis_out_t> &m_axis_result, hls::stream<ap_uint<64> > &tag_s) {
  psa_t psa_l = 0;
  psa_t psa_w = 0;

BIN_LOOP:
  for (unsigned k = 0; k < N_SAMPLES; k++) {
#pragma HLS pipeline II = 1
    fft_cplx_t xk = xk_s.read();
    bin_mag_t asdm = fixed_abs(xk.real()) + fixed_abs(xk.imag());
    if (k >= psa_l_lo && k <= psa_l_hi)
      psa_l += asdm;
    if (k >= psa_w_lo && k <= psa_w_hi)
      psa_w += asdm;
  }
  // Block-floating-point exponent is shared by every bin in the frame, so it cancels exactly in
  // the FCI ratio computed downstream in software; drained here only to balance the dataflow FIFO.
  status_s.read();

  // The event tag axis_to_fft captured from the input frame, forwarded unchanged to both result
  // beats. One read serves both beats: the tag identifies the EVENT, not the beat.
  ap_uint<64> tag = tag_s.read();

  axis_out_t beat_l;
  beat_l.data = 0;
  beat_l.data.range(27, 0) = psa_l.range(27, 0);
  beat_l.keep = -1;
  beat_l.strb = -1;
  beat_l.last = 0;
  beat_l.user = tag;
  m_axis_result.write(beat_l);

  axis_out_t beat_w;
  beat_w.data = 0;
  beat_w.data.range(27, 0) = psa_w.range(27, 0);
  beat_w.keep = -1;
  beat_w.strb = -1;
  beat_w.last = 1;
  beat_w.user = tag;
  m_axis_result.write(beat_w);
}

void fci_core(hls::stream<axis_in_t> &s_axis_data, hls::stream<axis_out_t> &m_axis_result,
              ap_uint<BIN_INDEX_WIDTH> psa_l_lo, ap_uint<BIN_INDEX_WIDTH> psa_l_hi,
              ap_uint<BIN_INDEX_WIDTH> psa_w_lo, ap_uint<BIN_INDEX_WIDTH> psa_w_hi) {
#pragma HLS interface axis port = s_axis_data
#pragma HLS interface axis port = m_axis_result
#pragma HLS interface s_axilite port = psa_l_lo bundle = control
#pragma HLS interface s_axilite port = psa_l_hi bundle = control
#pragma HLS interface s_axilite port = psa_w_lo bundle = control
#pragma HLS interface s_axilite port = psa_w_hi bundle = control
#pragma HLS interface s_axilite port = return bundle = control

#pragma HLS dataflow

  hls::stream<fft_cplx_t> xn_s;
  hls::stream<fft_cplx_t> xk_s;
  hls::stream<hls::ip_fft::status_t<fci_fft_config> > status_s;
  hls::stream<hls::ip_fft::config_t<fci_fft_config> > config_s;
  // Carries the event timestamp from axis_to_fft to fft_to_psa, around the FFT IP rather than
  // through it -- see axis_to_fft's comment. depth=2 matches config_s/status_s: one value produced
  // and consumed per frame, not per sample.
  hls::stream<ap_uint<64> > tag_s;
#pragma HLS stream variable = xn_s depth = 4
#pragma HLS stream variable = xk_s depth = 4
#pragma HLS stream variable = status_s depth = 2
#pragma HLS stream variable = config_s depth = 2
#pragma HLS stream variable = tag_s depth = 2

  axis_to_fft(s_axis_data, xn_s, config_s, tag_s);
  hls::fft<fci_fft_config>(xn_s, xk_s, status_s, config_s);
  fft_to_psa(xk_s, status_s, psa_l_lo, psa_l_hi, psa_w_lo, psa_w_hi, m_axis_result, tag_s);
}
