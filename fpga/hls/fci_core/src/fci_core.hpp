// FCI core: streaming Frequency Classification Index front-end.
//
// Implements the spectral part of Morales et al., "Gamma/neutron classification
// with SiPM CLYC detectors using frequency-domain analysis for embedded
// real-time applications", Nucl. Eng. Technol. 56 (2024) 745-752, Eqs. 2-4,
// adapted to this board's LTC2248 ADC (14-bit @ 50 Msps): the paper's
// 2048-sample/100 Msps FFT window decimated by 2 to 1024 samples/50 Msps
// (same 20.48 us window, same bin-to-Hz mapping, see the project plan).
//
// This core does NOT compute the final FCI ratio: it streams out PSA_l and
// PSA_w per event and leaves the division to the MicroBlaze (see project plan
// for the resource/latency rationale).
#ifndef FCI_CORE_HPP
#define FCI_CORE_HPP

#include "ap_axi_sdata.h"
#include "ap_fixed.h"
#include "ap_int.h"
#include "hls_fft.h"
#include "hls_stream.h"

// ---------------------------------------------------------------------------
// Trace / ADC parameters (this board: LTC2248, 14-bit @ 50 Msps)
// ---------------------------------------------------------------------------
static const unsigned ADC_WIDTH = 14;
static const unsigned N_SAMPLES = 1024;     // 2^10, decimated from the paper's 2048 @ 100 Msps
static const unsigned NFFT = 10;            // log2(N_SAMPLES)
static const unsigned BIN_INDEX_WIDTH = 10; // enough for bin indices 0..1023

// ---------------------------------------------------------------------------
// FFT configuration (hls::ip_fft, wraps Xilinx LogiCORE FFT v9.1)
// ---------------------------------------------------------------------------
struct fci_fft_config : hls::ip_fft::params_t {
  static const unsigned max_nfft = NFFT;                             // 1024-point FFT
  static const unsigned input_width = 16;                            // 14-bit ADC code, left-justified
  static const unsigned output_width = 16;                           // must equal input_width (scaled/BFP)
  static const unsigned config_width = 8;                            // direction bit only (no scaling sch.)
  static const unsigned ordering_opt = hls::ip_fft::natural_order;   // bin k == output index k
  static const unsigned scaling_opt = hls::ip_fft::block_floating_point; // single shared exponent/frame,
                                                                          // cancels exactly in FCI ratio
  static const unsigned rounding_opt = hls::ip_fft::convergent_rounding;
  // channels = 1, arch_opt = pipelined_streaming_io, has_nfft = false: inherited defaults, already correct
};

typedef ap_fixed<16, 1> fft_sample_t; // normalized fraction in [-1,1)
typedef std::complex<fft_sample_t> fft_cplx_t;

// ASDM per bin: |Re|+|Im|, each term in [0,1] -> sum in [0,2]; 2 integer bits for headroom at the
// exact-1.0-plus-1.0 corner case.
typedef ap_ufixed<18, 2> bin_mag_t;

// PSA accumulators / streamed-out results: worst case is the full 1023 non-DC bins summed
// (window bounds are runtime-configurable, so this can't assume the paper's <=90-bin span).
// 1023 * <2.0 < 2046 -> 11 integer bits needed; 12 used for headroom. Q12.16 fixed-point.
typedef ap_ufixed<28, 12> psa_t;

// ---------------------------------------------------------------------------
// AXI4-Stream port types
// ---------------------------------------------------------------------------
typedef ap_axiu<16, 1, 1, 1> axis_in_t;  // one ADC sample per beat (14 valid bits, zero-extended)
typedef ap_axiu<32, 1, 1, 1> axis_out_t; // PSA_l then PSA_w per event, Q12.16 zero-extended to 32 bits

void fci_core(hls::stream<axis_in_t> &s_axis_data, hls::stream<axis_out_t> &m_axis_result,
              ap_uint<BIN_INDEX_WIDTH> psa_l_lo, ap_uint<BIN_INDEX_WIDTH> psa_l_hi,
              ap_uint<BIN_INDEX_WIDTH> psa_w_lo, ap_uint<BIN_INDEX_WIDTH> psa_w_hi);

#endif // FCI_CORE_HPP
