/*
 * cli.c
 *
 * Command parser for the ASCII interface described in cli.h and docs/CLI_documentation.md.
 *
 * Structure: a byte pump assembles lines from the UART, a tokenizer turns the tail into signed
 * 32-bit arguments, and a dispatch table routes the two-character code to a handler. Handlers own
 * their reply, because a get and a set of the same parameter answer differently.
 *
 * Configuration is read back from the cores' own registers rather than from firmware shadows
 * wherever the register is readable. Shadows drift the moment anything else writes a register --
 * bringup, an interrupt handler, a future scheduler -- and a CLI that confidently reports a stale
 * value is worse than one that cannot report at all. The two exceptions are documented at their
 * declarations below.
 */

#include "cli.h"

#include <stdlib.h>

#include "acquisition.h"
#include "blr.h"
#include "fci_sink.h"
#include "psd.h"
#include "registers.h"
#include "vga_dac.h"
#include "xil_io.h"
#include "xil_printf.h"
#include "xuartlite_l.h"

/* fci_sink is not in every block design: while fci_core's results still travel over axi_dma_0 there
 * is no XPAR_FCI_SINK_0_BASEADDR, and acquisition.c compiles to nothing. registers.h guards the
 * base address the same way. The CLI stays useful in that build -- every configuration command and
 * $RT work unchanged -- and the result-path commands report their absence rather than pretending
 * the FIFO is merely empty. */
#ifdef XPAR_FCI_SINK_0_BASEADDR
#define CLI_HAVE_RESULTS 1
#else
#define CLI_HAVE_RESULTS 0
#endif

#define CLI_LINE_MAX 96
#define CLI_ARGS_MAX 8

#define ERR_UNKNOWN 0
#define ERR_PARAM 1

static char g_line[CLI_LINE_MAX];
static u32 g_len;
static int g_truncated; /* a line ran past CLI_LINE_MAX; reject it rather than act on a fragment */

static CliTraceFn g_trace_fn;

static int g_running;
#if CLI_HAVE_RESULTS
static AcqStats g_stats;
#endif

/* Shadow 1 of 2: the AD8330 gain DACs are write-only over I2C, so $GV can only report what was last
 * commanded. Seeded with the power-on defaults from vga_dac.h. */
static s32 g_vga_fine_milli = (s32)(AD8330_DEFAULT_GAIN_FINE_LINEAR * 1000.0);
static s32 g_vga_coarse_milli = (s32)(AD8330_DEFAULT_GAIN_COARSE_LINEAR * 1000.0);
static s32 g_vga_raw_code = -1; /* -1 until a raw code is written */

/* Shadow 2 of 2: the pulse shaper is not in the FPGA deployment yet, but issue #15 asks for its
 * commands so host-side software can be written against them now. These are stored and reported and
 * applied to nothing. $GH reports a leading `present` flag of 0 so a host cannot mistake a shadowed
 * setting for a live one. */
static s32 g_shaper[4]; /* peaking, gap, decay, enable */

/* ---------------------------------------------------------------- reply helpers */

static void reply_err(int code) { xil_printf("!XX %d\n", code); }
static void reply_ack(const char *c) { xil_printf("!%c%c\n", c[0], c[1]); }
static void reply_open(const char *c) { xil_printf("!%c%c", c[0], c[1]); }
static void reply_val(s32 v) { xil_printf(" %d", v); }
static void reply_close(void) { xil_printf("\n"); }

static void reply_one(const char *c, s32 v) {
  reply_open(c);
  reply_val(v);
  reply_close();
}

/* A get that took a selector echoes it, so `!GB 1 256` is readable without the request beside it. */
static void reply_sel(const char *c, s32 sel, s32 v) {
  reply_open(c);
  reply_val(sel);
  reply_val(v);
  reply_close();
}

/* ---------------------------------------------------------------- register helpers */

static u32 reg_get(u32 base, u32 off) { return Xil_In32(base + off); }
static void reg_set(u32 base, u32 off, u32 v) { Xil_Out32(base + off, v); }

/* Sign-extends the low `bits` of a register word. The signed registers (trigger threshold,
 * psd baseline_ref) are narrower than 32 bits, so a raw read reports a large positive number where
 * a small negative one was written. */
static s32 sign_extend(u32 v, int bits) {
  u32 m = 1u << (bits - 1);
  v &= (bits >= 32) ? 0xFFFFFFFFu : ((1u << bits) - 1u);
  return (s32)((v ^ m) - m);
}

static int in_range(s32 v, s32 lo, s32 hi) { return v >= lo && v <= hi; }

/* ---------------------------------------------------------------- trigger_core
 *
 * trigger_core has no driver of its own; bringup.c writes its registers directly. Rather than
 * introduce a second writer with its own idea of the layout, the accessors stay here and are the
 * only ones in this file. Factoring a trigger.c/h out of both is worth doing, but belongs with a
 * change to bringup.c rather than to the CLI. */

#define TRG_THRESHOLD 0
#define TRG_POLARITY 1
#define TRG_DELAY 2
#define TRG_DEPTH 3

static int trg_get(s32 idx, s32 *out) {
  switch (idx) {
  case TRG_THRESHOLD:
    /* Signed since the datapath became signed end to end (log 8a) -- 16-bit, not the 14-bit
     * offset-binary code the register map originally described. */
    *out = sign_extend(reg_get(TRIGGER_CORE_BASEADDR, TRIGGER_CORE_THRESHOLD_OFFSET), 16);
    return 1;
  case TRG_POLARITY:
    *out = (s32)(reg_get(TRIGGER_CORE_BASEADDR, TRIGGER_CORE_POLARITY_OFFSET) & 1u);
    return 1;
  case TRG_DELAY:
    *out = (s32)(reg_get(TRIGGER_CORE_BASEADDR, TRIGGER_CORE_DELAY_OFFSET) & 0x1FFu);
    return 1;
  case TRG_DEPTH:
    *out = (s32)(reg_get(TRIGGER_CORE_BASEADDR, TRIGGER_CORE_DEPTH_OFFSET) & 0x1FFFu);
    return 1;
  default:
    return 0;
  }
}

static int trg_set(s32 idx, s32 v) {
  switch (idx) {
  case TRG_THRESHOLD:
    if (!in_range(v, -32768, 32767))
      return 0;
    reg_set(TRIGGER_CORE_BASEADDR, TRIGGER_CORE_THRESHOLD_OFFSET, (u32)v & 0xFFFFu);
    return 1;
  case TRG_POLARITY:
    if (!in_range(v, 0, 1))
      return 0;
    reg_set(TRIGGER_CORE_BASEADDR, TRIGGER_CORE_POLARITY_OFFSET, (u32)v);
    return 1;
  case TRG_DELAY:
    /* The core clamps to 2..256 in hardware; rejecting here instead means a mistyped value is
     * reported rather than silently corrected into something that still acquires. */
    if (!in_range(v, 2, 256))
      return 0;
    reg_set(TRIGGER_CORE_BASEADDR, TRIGGER_CORE_DELAY_OFFSET, (u32)v);
    return 1;
  case TRG_DEPTH:
    if (!in_range(v, 1, CLI_TRACE_MAX))
      return 0;
    reg_set(TRIGGER_CORE_BASEADDR, TRIGGER_CORE_DEPTH_OFFSET, (u32)v);
    return 1;
  default:
    return 0;
  }
}

/* ---------------------------------------------------------------- blr_core */

static int blr_get(s32 idx, s32 *out) {
  u32 ctrl = reg_get(BLR_CORE_BASEADDR, BLR_CTRL_OFFSET);
  switch (idx) {
  case 0: *out = (s32)(reg_get(BLR_CORE_BASEADDR, BLR_SHIFT_OFFSET) & 0xFu); return 1;
  case 1: *out = (s32)(reg_get(BLR_CORE_BASEADDR, BLR_GATE_THR_OFFSET) & 0x3FFFu); return 1;
  case 2: *out = (s32)(reg_get(BLR_CORE_BASEADDR, BLR_HOLDOFF_OFFSET) & 0xFFFu); return 1;
  case 3: *out = (s32)(ctrl & 1u); return 1;
  case 4: *out = (s32)((ctrl >> 1) & 1u); return 1;
  case 5: *out = Blr_GetBaseline(BLR_CORE_BASEADDR); return 1; /* read-only */
  case 6: *out = Blr_GateOpen(BLR_CORE_BASEADDR); return 1;    /* read-only */
  default: return 0;
  }
}

static int blr_set(s32 idx, s32 v) {
  switch (idx) {
  case 0:
    if (!in_range(v, 0, 15))
      return 0;
    reg_set(BLR_CORE_BASEADDR, BLR_SHIFT_OFFSET, (u32)v);
    return 1;
  case 1:
    if (!in_range(v, 0, 16383))
      return 0;
    reg_set(BLR_CORE_BASEADDR, BLR_GATE_THR_OFFSET, (u32)v);
    return 1;
  case 2:
    if (!in_range(v, 0, 4095))
      return 0;
    reg_set(BLR_CORE_BASEADDR, BLR_HOLDOFF_OFFSET, (u32)v);
    return 1;
  case 3:
    if (!in_range(v, 0, 1))
      return 0;
    Blr_SetBypass(BLR_CORE_BASEADDR, (int)v);
    return 1;
  case 4:
    if (!in_range(v, 0, 1))
      return 0;
    Blr_SetHold(BLR_CORE_BASEADDR, (int)v);
    return 1;
  default:
    return 0; /* 5 and 6 are read-only */
  }
}

/* ---------------------------------------------------------------- psd_core */

static const u32 psd_off[] = {PSD_PRE_TRIGGER_OFFSET, PSD_PRE_GATE_OFFSET, PSD_SHORT_GATE_OFFSET,
                              PSD_LONG_GATE_OFFSET, PSD_BASELINE_REF_OFFSET, PSD_WATERMARK_OFFSET};

static int psd_get(s32 idx, s32 *out) {
  if (idx < 0 || idx > 5)
    return 0;
  if (idx == 4) {
    *out = sign_extend(reg_get(PSD_CORE_BASEADDR, PSD_BASELINE_REF_OFFSET), 16);
    return 1;
  }
  *out = (s32)reg_get(PSD_CORE_BASEADDR, psd_off[idx]);
  return 1;
}

static int psd_set(s32 idx, s32 v) {
  switch (idx) {
  case 0: case 1: case 2: case 3:
    if (!in_range(v, 0, 65535))
      return 0;
    reg_set(PSD_CORE_BASEADDR, psd_off[idx], (u32)v);
    return 1;
  case 4:
    if (!in_range(v, -32768, 32767))
      return 0;
    reg_set(PSD_CORE_BASEADDR, PSD_BASELINE_REF_OFFSET, (u32)v & 0xFFFFu);
    return 1;
  case 5:
    if (!in_range(v, 0, 32))
      return 0;
    Psd_SetWatermark(PSD_CORE_BASEADDR, (u32)v);
    return 1;
  default:
    return 0;
  }
}

/* ---------------------------------------------------------------- fci_core / fci_sink */

static const u32 fci_off[] = {FCI_CORE_PSA_L_LO_OFFSET, FCI_CORE_PSA_L_HI_OFFSET,
                              FCI_CORE_PSA_W_LO_OFFSET, FCI_CORE_PSA_W_HI_OFFSET};

static int fci_get(s32 idx, s32 *out) {
  if (idx >= 0 && idx <= 3) {
    *out = (s32)reg_get(FCI_CORE_BASEADDR, fci_off[idx]);
    return 1;
  }
#if CLI_HAVE_RESULTS
  if (idx == 4) {
    *out = (s32)reg_get(FCI_SINK_BASEADDR, FCI_SINK_WATERMARK_OFFSET);
    return 1;
  }
#endif
  return 0;
}

static int fci_set(s32 idx, s32 v) {
  if (idx >= 0 && idx <= 3) {
    /* Bin indices into the FFT magnitude spectrum. The upper bound is the Nyquist bin of the
     * 1024-point transform; the core does not range-check these itself. */
    if (!in_range(v, 0, 512))
      return 0;
    reg_set(FCI_CORE_BASEADDR, fci_off[idx], (u32)v);
    return 1;
  }
#if CLI_HAVE_RESULTS
  if (idx == 4) {
    if (!in_range(v, 0, 32))
      return 0;
    FciSink_SetWatermark(FCI_SINK_BASEADDR, (u32)v);
    return 1;
  }
#endif
  return 0;
}

/* ---------------------------------------------------------------- VGA (AD8330 over I2C) */

static int vga_get(s32 idx, s32 *out) {
  switch (idx) {
  case 0: *out = g_vga_fine_milli; return 1;
  case 1: *out = g_vga_coarse_milli; return 1;
  case 2: *out = g_vga_raw_code; return 1;
  default: return 0;
  }
}

static int vga_set(s32 idx, s32 v) {
  /* Gains travel as milli-units because the framing carries integers only: 1500 means x1.50. */
  switch (idx) {
  case 0:
    if (!in_range(v, 1, 60000))
      return 0;
    if (VgaDac_SetGainFine((double)v / 1000.0) != 0)
      return 0;
    g_vga_fine_milli = v;
    return 1;
  case 1:
    if (!in_range(v, 1, 60000))
      return 0;
    if (VgaDac_SetGainCoarse((double)v / 1000.0) != 0)
      return 0;
    g_vga_coarse_milli = v;
    return 1;
  case 2:
    if (!in_range(v, 0, 4095))
      return 0;
    if (VgaDac_SetFineCodeRaw((u16)v) != 0)
      return 0;
    g_vga_raw_code = v;
    return 1;
  default:
    return 0;
  }
}

/* ---------------------------------------------------------------- handlers */

typedef int (*CliHandler)(const char *code, const s32 *a, int n);

/* Shared shape for the six indexed getters: no argument dumps every parameter, one argument reads
 * one. Dumping is what makes a configuration reproducible from a terminal log. */
static int generic_get(const char *code, const s32 *a, int n, int count,
                       int (*get)(s32, s32 *)) {
  s32 v;
  int i;
  if (n == 0) {
    reply_open(code);
    for (i = 0; i < count; i++) {
      if (get((s32)i, &v))
        reply_val(v);
    }
    reply_close();
    return 0;
  }
  if (n != 1 || !get(a[0], &v))
    return ERR_PARAM;
  reply_sel(code, a[0], v);
  return 0;
}

static int generic_set(const char *code, const s32 *a, int n, int (*set)(s32, s32)) {
  if (n != 2 || !set(a[0], a[1]))
    return ERR_PARAM;
  reply_ack(code);
  return 0;
}

static int h_gt(const char *c, const s32 *a, int n) { return generic_get(c, a, n, 4, trg_get); }
static int h_st(const char *c, const s32 *a, int n) { return generic_set(c, a, n, trg_set); }
static int h_gb(const char *c, const s32 *a, int n) { return generic_get(c, a, n, 7, blr_get); }
static int h_sb(const char *c, const s32 *a, int n) { return generic_set(c, a, n, blr_set); }
static int h_gp(const char *c, const s32 *a, int n) { return generic_get(c, a, n, 6, psd_get); }
static int h_sp(const char *c, const s32 *a, int n) { return generic_set(c, a, n, psd_set); }
static int h_gf(const char *c, const s32 *a, int n) {
  return generic_get(c, a, n, CLI_HAVE_RESULTS ? 5 : 4, fci_get);
}
static int h_sf(const char *c, const s32 *a, int n) { return generic_set(c, a, n, fci_set); }
static int h_gv(const char *c, const s32 *a, int n) { return generic_get(c, a, n, 3, vga_get); }
static int h_sv(const char *c, const s32 *a, int n) { return generic_set(c, a, n, vga_set); }

/* The shaper answers with a leading 0 in the no-argument dump: that field is `present`, and it will
 * read 1 once the core is in the FPGA. A host must check it before trusting the rest. */
static int h_gh(const char *c, const s32 *a, int n) {
  int i;
  if (n == 0) {
    reply_open(c);
    reply_val(0);
    for (i = 0; i < 4; i++)
      reply_val(g_shaper[i]);
    reply_close();
    return 0;
  }
  if (n != 1 || !in_range(a[0], 0, 3))
    return ERR_PARAM;
  reply_sel(c, a[0], g_shaper[a[0]]);
  return 0;
}

static int h_sh(const char *c, const s32 *a, int n) {
  if (n != 2 || !in_range(a[0], 0, 3) || a[1] < 0)
    return ERR_PARAM;
  g_shaper[a[0]] = a[1];
  reply_ack(c);
  return 0;
}

static int h_ping(const char *c, const s32 *a, int n) {
  (void)a;
  if (n != 0)
    return ERR_PARAM;
  reply_ack(c);
  return 0;
}

static int h_id(const char *c, const s32 *a, int n) {
  (void)a;
  if (n != 0)
    return ERR_PARAM;
  xil_printf("!%c%c FCI-FPGA 1 0\n", c[0], c[1]); /* name, protocol major, minor */
  return 0;
}

static int h_ae(const char *c, const s32 *a, int n) {
  (void)a;
  if (n != 0)
    return ERR_PARAM;
  Psd_Clear(PSD_CORE_BASEADDR);
#if CLI_HAVE_RESULTS
  FciSink_Clear(FCI_SINK_BASEADDR);
  Acq_ResetStats(&g_stats);
#endif
  g_running = 1;
  reply_ack(c);
  return 0;
}

static int h_ad(const char *c, const s32 *a, int n) {
  (void)a;
  if (n != 0)
    return ERR_PARAM;
  g_running = 0;
  reply_ack(c);
  return 0;
}

static int h_es(const char *c, const s32 *a, int n) {
  (void)a;
  if (n != 0)
    return ERR_PARAM;
  reply_one(c, g_running);
  return 0;
}

static int h_ar(const char *c, const s32 *a, int n) {
  (void)a;
  if (n != 0)
    return ERR_PARAM;
  Psd_Clear(PSD_CORE_BASEADDR);
#if CLI_HAVE_RESULTS
  FciSink_Clear(FCI_SINK_BASEADDR);
  Acq_ResetStats(&g_stats);
#endif
  reply_ack(c);
  return 0;
}

/* Pops one event matched across both result FIFOs. The leading field is a validity flag: 0 means
 * nothing was pending, and no further values follow. Without it an empty reply would be
 * indistinguishable from a malformed one. The timestamp is split lo/hi because the framing carries
 * 32-bit values, the same split the reference CLI uses for its 64-bit integrals. */
static int h_rv(const char *c, const s32 *a, int n) {
  (void)a;
  if (n != 0)
    return ERR_PARAM;
#if CLI_HAVE_RESULTS
  {
  AcqEvent ev;
  if (!g_running || !Acq_PopPaired(&ev, &g_stats)) {
    reply_one(c, 0);
    return 0;
  }
  reply_open(c);
  reply_val(1);
  reply_val((s32)(u32)(ev.timestamp & 0xFFFFFFFFu));
  reply_val((s32)(u32)(ev.timestamp >> 32));
  reply_val((s32)ev.psa_l);
  reply_val((s32)ev.psa_w);
  reply_val((s32)ev.fci_scaled);
  reply_val(ev.energy_short);
  reply_val(ev.energy_long);
  reply_val(ev.psd_scaled);
  reply_close();
  }
#else
  /* -1, not 0: a host must be able to tell "this bitstream has no result path" from
   * "nothing is pending right now". */
  reply_one(c, -1);
#endif
  return 0;
}

/* FIFO occupancy on both sides, so a host can size its polling instead of guessing. */
static int h_rn(const char *c, const s32 *a, int n) {
  (void)a;
  if (n != 0)
    return ERR_PARAM;
  reply_open(c);
#if CLI_HAVE_RESULTS
  reply_val((s32)FciSink_Level(FCI_SINK_BASEADDR));
#else
  reply_val(-1);
#endif
  reply_val((s32)Psd_Level(PSD_CORE_BASEADDR));
  reply_close();
  return 0;
}

static int h_rs(const char *c, const s32 *a, int n) {
  (void)a;
  if (n != 0)
    return ERR_PARAM;
  reply_open(c);
#if !CLI_HAVE_RESULTS
  /* Pairing statistics only exist where there is something to pair against. */
  reply_val(-1); reply_val(-1); reply_val(-1);
  reply_val(-1); reply_val(-1); reply_val(-1);
  reply_close();
  return 0;
#else
  reply_val((s32)g_stats.paired);
  reply_val((s32)g_stats.dropped_fci);
  reply_val((s32)g_stats.dropped_psd);
  reply_val((s32)g_stats.fci_overflows);
  reply_val((s32)g_stats.psd_overflows);
  reply_val((s32)g_stats.fci_framing_errors);
  reply_close();
  return 0;
#endif
}

/* Captures one raw trace and returns it as `count` followed by that many signed samples. Long, but
 * a trace is inherently long, and splitting it across replies would need a sequencing scheme the
 * framing does not have. */
static int h_rt(const char *c, const s32 *a, int n) {
  u32 count = 0, i, want = CLI_TRACE_MAX;
  const s16 *trace = 0;
  if (n > 1)
    return ERR_PARAM;
  if (n == 1) {
    if (!in_range(a[0], 1, CLI_TRACE_MAX))
      return ERR_PARAM;
    want = (u32)a[0];
  }
  if (g_trace_fn == 0 || !g_trace_fn(&trace, want, &count)) {
    reply_one(c, 0);
    return 0;
  }
  reply_open(c);
  reply_val((s32)count);
  for (i = 0; i < count; i++)
    reply_val((s32)trace[i]);
  reply_close();
  return 0;
}

static const struct {
  char code[2];
  CliHandler fn;
} g_cmds[] = {
    {{'~', '~'}, h_ping}, {{'I', 'D'}, h_id},  {{'A', 'E'}, h_ae},  {{'A', 'D'}, h_ad},
    {{'E', 'S'}, h_es},   {{'A', 'R'}, h_ar},  {{'R', 'V'}, h_rv},  {{'R', 'N'}, h_rn},
    {{'R', 'S'}, h_rs},   {{'R', 'T'}, h_rt},  {{'G', 'T'}, h_gt},  {{'S', 'T'}, h_st},
    {{'G', 'B'}, h_gb},   {{'S', 'B'}, h_sb},  {{'G', 'P'}, h_gp},  {{'S', 'P'}, h_sp},
    {{'G', 'F'}, h_gf},   {{'S', 'F'}, h_sf},  {{'G', 'V'}, h_gv},  {{'S', 'V'}, h_sv},
    {{'G', 'H'}, h_gh},   {{'S', 'H'}, h_sh},
};

#define CLI_CMD_COUNT ((int)(sizeof(g_cmds) / sizeof(g_cmds[0])))

/* ---------------------------------------------------------------- parsing */

static int parse_args(char *p, s32 *out) {
  int n = 0;
  char *end;
  long v;
  while (*p) {
    while (*p == ' ' || *p == '\t')
      p++;
    if (*p == '\0')
      break;
    if (n >= CLI_ARGS_MAX)
      return -1;
    v = strtol(p, &end, 0); /* base 0 so 0x... is accepted for masks */
    if (end == p)
      return -1;
    out[n++] = (s32)v;
    p = end;
  }
  return n;
}

static void execute(char *line) {
  s32 args[CLI_ARGS_MAX];
  int n, i;

  if (line[0] != '$' || line[1] == '\0' || line[2] == '\0') {
    /* Too short to carry a code at all: unknown rather than bad-parameter, since nothing was
     * successfully identified. */
    reply_err(ERR_UNKNOWN);
    return;
  }
  n = parse_args(line + 3, args);
  if (n < 0) {
    reply_err(ERR_PARAM);
    return;
  }
  for (i = 0; i < CLI_CMD_COUNT; i++) {
    if (g_cmds[i].code[0] == line[1] && g_cmds[i].code[1] == line[2]) {
      int rc = g_cmds[i].fn(g_cmds[i].code, args, n);
      if (rc != 0)
        reply_err(rc);
      return;
    }
  }
  reply_err(ERR_UNKNOWN);
}

/* ---------------------------------------------------------------- public */

void Cli_SetTraceProvider(CliTraceFn fn) { g_trace_fn = fn; }

void Cli_Init(void) {
  g_len = 0;
  g_truncated = 0;
  g_running = 0;
#if CLI_HAVE_RESULTS
  Acq_ResetStats(&g_stats);
#endif
}

int Cli_AcquisitionEnabled(void) { return g_running; }

void Cli_Poll(void) {
  while (!XUartLite_IsReceiveEmpty(AXI_UARTLITE_BASEADDR)) {
    char ch = (char)XUartLite_RecvByte(AXI_UARTLITE_BASEADDR);
    if (ch == '\r')
      continue; /* terminals that send CRLF */
    if (ch != '\n') {
      if (g_len < CLI_LINE_MAX - 1)
        g_line[g_len++] = ch;
      else
        g_truncated = 1;
      continue;
    }
    g_line[g_len] = '\0';
    if (g_truncated)
      reply_err(ERR_PARAM); /* never act on the head of an over-long line */
    else if (g_len > 0)
      execute(g_line);
    g_len = 0;
    g_truncated = 0;
    return; /* one command per call, so input cannot starve the caller's loop */
  }
}
