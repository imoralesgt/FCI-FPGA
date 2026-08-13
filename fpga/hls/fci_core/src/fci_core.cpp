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
                         hls::stream<hls::ip_fft::config_t<fci_fft_config> > &config_s) {
  hls::ip_fft::config_t<fci_fft_config> cfg;
  cfg.setDir(1); // forward transform
  config_s.write(cfg);

SAMPLE_LOOP:
  for (unsigned i = 0; i < N_SAMPLES; i++) {
#pragma HLS pipeline II = 1
    axis_in_t beat = s_axis_data.read();
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
}

static void fft_to_psa(hls::stream<fft_cplx_t> &xk_s,
                        hls::stream<hls::ip_fft::status_t<fci_fft_config> > &status_s,
                        hls::stream<ap_uint<BIN_INDEX_WIDTH> > &psa_l_lo_s,
                        hls::stream<ap_uint<BIN_INDEX_WIDTH> > &psa_l_hi_s,
                        hls::stream<ap_uint<BIN_INDEX_WIDTH> > &psa_w_lo_s,
                        hls::stream<ap_uint<BIN_INDEX_WIDTH> > &psa_w_hi_s,
                        hls::stream<axis_out_t> &m_axis_result) {
  psa_t psa_l = 0;
  psa_t psa_w = 0;

  // Window bounds read once per frame from plain hls::stream ports (not scalar ap_none/s_axilite
  // ports): cosim_design refuses ap_ctrl_none dataflow designs that have ANY scalar port at all
  // (COSIM 212-345 -- confirmed empirically, its restriction turned out to apply to ap_none
  // scalars just as much as s_axilite ones, not just s_axilite as originally assumed), but
  // explicitly allows hls_stream/AXI4-stream ports. An external always-ready producer (e.g. the
  // AXI4-Lite register block) just needs to keep offering the current register value on these
  // streams; reading the same value repeatedly is harmless since it changes only when
  // reconfigured.
  ap_uint<BIN_INDEX_WIDTH> psa_l_lo = psa_l_lo_s.read();
  ap_uint<BIN_INDEX_WIDTH> psa_l_hi = psa_l_hi_s.read();
  ap_uint<BIN_INDEX_WIDTH> psa_w_lo = psa_w_lo_s.read();
  ap_uint<BIN_INDEX_WIDTH> psa_w_hi = psa_w_hi_s.read();

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

  axis_out_t beat_l;
  beat_l.data = 0;
  beat_l.data.range(27, 0) = psa_l.range(27, 0);
  beat_l.keep = -1;
  beat_l.strb = -1;
  beat_l.last = 0;
  m_axis_result.write(beat_l);

  axis_out_t beat_w;
  beat_w.data = 0;
  beat_w.data.range(27, 0) = psa_w.range(27, 0);
  beat_w.keep = -1;
  beat_w.strb = -1;
  beat_w.last = 1;
  m_axis_result.write(beat_w);
}

void fci_core(hls::stream<axis_in_t> &s_axis_data, hls::stream<axis_out_t> &m_axis_result,
              hls::stream<ap_uint<BIN_INDEX_WIDTH> > &psa_l_lo_s,
              hls::stream<ap_uint<BIN_INDEX_WIDTH> > &psa_l_hi_s,
              hls::stream<ap_uint<BIN_INDEX_WIDTH> > &psa_w_lo_s,
              hls::stream<ap_uint<BIN_INDEX_WIDTH> > &psa_w_hi_s) {
#pragma HLS interface axis port = s_axis_data
#pragma HLS interface axis port = m_axis_result
// Window bounds arrive as plain hls::stream (ap_fifo) ports, not scalar ap_none/s_axilite ports:
// keeps every port on this ap_ctrl_none/free-running top function a streaming port, which
// cosim_design requires (COSIM 212-345 -- confirmed empirically that it rejects ANY scalar port,
// ap_none included, on an ap_ctrl_none dataflow design; see fft_to_psa for detail). This keeps
// the top-level control protocol free-running so frame N+1 can start streaming into axis_to_fft
// while frame N is still draining through the FFT/fft_to_psa stages (ap_ctrl_hs's start/done
// handshake was measured via cosim to force Interval == Latency; pure ap_ctrl_none measured
// Interval down to 1024 cycles, the streaming floor). Runtime configurability of the window
// bounds is retained via a separate hand-written AXI4-Lite register block
// (fpga/rtl/trigger_core/src/axi4lite_regs.vhd is the reference pattern), whose 4 output
// registers continuously drive these 4 streams (always-valid producer; the same register value
// is read again every frame until reconfigured).
#pragma HLS interface ap_fifo port = psa_l_lo_s
#pragma HLS interface ap_fifo port = psa_l_hi_s
#pragma HLS interface ap_fifo port = psa_w_lo_s
#pragma HLS interface ap_fifo port = psa_w_hi_s
#pragma HLS interface ap_ctrl_none port = return

#pragma HLS dataflow

  hls::stream<fft_cplx_t> xn_s;
  hls::stream<fft_cplx_t> xk_s;
  hls::stream<hls::ip_fft::status_t<fci_fft_config> > status_s;
  hls::stream<hls::ip_fft::config_t<fci_fft_config> > config_s;
#pragma HLS stream variable = xn_s depth = 4
#pragma HLS stream variable = xk_s depth = 4
#pragma HLS stream variable = status_s depth = 2
#pragma HLS stream variable = config_s depth = 2

  axis_to_fft(s_axis_data, xn_s, config_s);
  hls::fft<fci_fft_config>(xn_s, xk_s, status_s, config_s);
  fft_to_psa(xk_s, status_s, psa_l_lo_s, psa_l_hi_s, psa_w_lo_s, psa_w_hi_s, m_axis_result);
}
