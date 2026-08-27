# FCI-FPGA — Command Line Interface Specification

**Target:** Digilent Cmod A7-35T, MicroBlaze soft CPU
**Interface:** Serial/UART over USB (AXI UARTLITE)
**Protocol:** Raw ASCII serial commands

## UART interface settings

| | |
|---|---|
| Baud rate | 921 600 bps |
| Data bits | 8 |
| Stop bits | 1 |
| Parity | None |
| Flow control | None |

## 1. ASCII command format

### 1.1 Request (host to DAQ)

`<Start character><Command code><Parameters><End character>`

- **Start character** — `$` (ASCII 0x24)
- **Command code** — two-character mnemonic
- **Parameters** — signed 32-bit values separated by spaces (ASCII 0x20). Values prefixed `0x` are
  read as hexadecimal.
- **End character** — `\n` (ASCII 0x0A). A preceding `\r` is ignored.

### 1.2 Reply (DAQ to host)

`<Start character><Command code><Values><End character>`

- **Start character** — `!` (ASCII 0x21)
- **Command code** — the same two characters as the request
- **Values** — payload, depending on the command
- **End character** — `\n` (ASCII 0x0A)

Two conventions apply throughout:

- A set command acknowledges with the bare code: `$SB 1 256\n` → `!SB\n`
- A get command echoes its selector before the value: `$GB 1\n` → `!GB 1 256\n`

### 1.3 Error replies

`!XX <error code>\n`

| code | meaning |
|---|---|
| 0 | command code not recognised |
| 1 | wrong number of parameters, or a value out of range |

Values outside the documented range are rejected, not clamped.

### 1.4 Transaction model

The interface is request/response. The DAQ transmits only in reply to a request; no message is sent
unsolicited. Acquisition results are retrieved with `$RV`.

---

## 2. System and acquisition commands

| command | parameters | reply | description |
|---|---|---|---|
| `$~~` | none | `!~~` | Ping. |
| `$ID` | none | `!ID <name> <major> <minor>` | Identification and protocol version. |
| `$AE` | none | `!AE` | Enable acquisition. Clears both result FIFOs and the statistics counters. |
| `$AD` | none | `!AD` | Disable acquisition. FIFO contents are retained. |
| `$ES` | none | `!ES <0\|1>` | Acquisition enable status. |
| `$AR` | none | `!AR` | Clear both result FIFOs and the statistics counters. Configuration is unchanged. |

### 2.1 Read Values (`$RV`)

Returns one event matched across the FCI and PSD result FIFOs by its hardware timestamp.

`!RV <valid> [ts_lo ts_hi psa_l psa_w fci energy_short energy_long psd]`

| field | description |
|---|---|
| `valid` | 1 if an event follows; 0 if none was pending; −1 if the result path is not present in the loaded bitstream. No further values follow 0 or −1. |
| `ts_lo`, `ts_hi` | 64-bit event timestamp, low word first, **unsigned decimal** |
| `psa_l`, `psa_w` | FCI window accumulators |
| `fci` | FCI ratio × 10 000 |
| `energy_short`, `energy_long` | PSD gate integrals, signed |
| `psd` | PSD parameter × 10 000 |

`$RV` replies `!RV 0` while acquisition is disabled.

```
$RV
!RV 1 2 1 111 222 5255 1000 4000 7500
```

### 2.2 Read pending (`$RN`)

`!RN <fci_level> <psd_level>` — number of results currently buffered in each FIFO. A field reads −1
if that result path is not present in the loaded bitstream.

### 2.3 Read statistics (`$RS`)

`!RS <paired> <dropped_fci> <dropped_psd> <ovf_fci> <ovf_psd> <framing_errors>`

All six fields are **unsigned decimal** counters, except that any field reads the literal value −1 when the result path is not present (see above) — that −1 is signed and is the one exception to this section.

| field | description |
|---|---|
| `paired` | events matched on both sides |
| `dropped_fci`, `dropped_psd` | results discarded while resynchronising |
| `ovf_fci`, `ovf_psd` | times a result FIFO reported full |
| `framing_errors` | FCI result frames received out of sequence |

All six fields read −1 if the FCI result path is not present in the loaded bitstream.

### 2.4 Read Counts (`$RC`)

`!RC <fci_event_count> <psd_event_count>` — total events each core has processed since its last
clear, **unsigned decimal**. `fci_event_count` reads −1 if the FCI result path is not present in
the loaded bitstream.

Unlike every other field in this section, these advance whether or not anything is popping either
FIFO — `$RS`'s counters only change inside a successful `$RV`, so polling `$RS` alone while never
calling `$RV` observes a value that cannot move. `$RC` is the field to poll for a live event rate.

### 2.5 Read Trace (`$RT`)

`$RT [n]` — captures one raw trace.

`!RT <count> <s0> <s1> ...`

`count` is 0 if no trace could be captured. `n` defaults to 2048. Samples are signed.

---

## 3. Configuration commands

Each subsystem uses one set and one get command: `$S<x> <index> <value>` and `$G<x> [index]`.
A get with no index returns every parameter of that subsystem in index order.

| subsystem | set | get |
|---|---|---|
| Trigger | `$ST` | `$GT` |
| Baseline restorer | `$SB` | `$GB` |
| PSD | `$SP` | `$GP` |
| FCI | `$SF` | `$GF` |
| VGA (AD8330, I²C) | `$SV` | `$GV` |
| Pulse shaper | `$SH` | `$GH` |

### 3.1 Trigger (`$ST` / `$GT`)

| index | parameter | range | notes |
|---|---|---|---|
| 0 | threshold | −32768 … 32767 | signed ADC code |
| 1 | polarity | 0 … 1 | 1 = rising crossing, 0 = falling crossing |
| 2 | delay | 2 … 256 | pre-trigger samples |
| 3 | depth | 1 … 2048 | capture length in samples |

### 3.2 Baseline restorer (`$SB` / `$GB`)

| index | parameter | range | notes |
|---|---|---|---|
| 0 | shift *k* | 0 … 15 | time constant = 2^*k* samples |
| 1 | gate threshold | 0 … 16383 | estimator freezes at or above this deviation |
| 2 | hold-off | 0 … 4095 | additional closed samples after the signal returns in range |
| 3 | bypass | 0 … 1 | 1 forwards the input unrestored |
| 4 | hold | 0 … 1 | 1 freezes the estimate |
| 5 | baseline | read-only | live signed estimate |
| 6 | gate open | read-only | 1 while the estimator is tracking |

Writing index 5 or 6 replies `!XX 1`.

### 3.3 PSD (`$SP` / `$GP`)

| index | parameter | range | notes |
|---|---|---|---|
| 0 | pre-trigger | 0 … 65535 | must equal the trigger delay (`$ST` index 2) |
| 1 | pre-gate | 0 … 65535 | samples before the trigger included in both gates |
| 2 | short gate | 0 … 65535 | short gate length in samples |
| 3 | long gate | 0 … 65535 | long gate length in samples |
| 4 | baseline reference | −32768 … 32767 | signed pedestal trim; 0 when fed by the baseline restorer |
| 5 | watermark | 0 … 32 | interrupt threshold; 0 disables |

### 3.4 FCI (`$SF` / `$GF`)

| index | parameter | range | notes |
|---|---|---|---|
| 0 | `psa_l` low bin | 0 … 512 | |
| 1 | `psa_l` high bin | 0 … 512 | |
| 2 | `psa_w` low bin | 0 … 512 | |
| 3 | `psa_w` high bin | 0 … 512 | |
| 4 | result watermark | 0 … 32 | interrupt threshold; 0 disables |

Bin indices address the 1024-point FFT magnitude spectrum. Bin 512 is the Nyquist bin.

Index 4 exists only when the FCI result path is present in the loaded bitstream. Where it is not,
`$SF 4` and `$GF 4` reply `!XX 1`, and `$GF` with no index returns four values instead of five.

### 3.5 VGA (`$SV` / `$GV`)

| index | parameter | range | notes |
|---|---|---|---|
| 0 | fine gain | 1 … 60000 | milli-units; 1500 = ×1.50 |
| 1 | coarse gain | 1 … 60000 | milli-units; 6000 = ×6.00 |
| 2 | fine DAC code | 0 … 4095 | raw 12-bit code |

The gain DACs are write-only. `$GV` returns the last value written, not a device reading. Index 2
returns −1 until a raw code has been written.

### 3.6 Pulse shaper (`$SH` / `$GH`)

`$GH` with no index replies `!GH <present> <peaking> <gap> <decay> <enable>`.

`present` is 0 when the shaper core is absent from the loaded bitstream and 1 when it is available.
While `present` is 0, values written with `$SH` are stored and returned by `$GH` but have no effect
on acquisition.

| index | parameter |
|---|---|
| 0 | peaking time (samples) |
| 1 | gap time (samples) |
| 2 | decay / pole-zero (samples) |
| 3 | enable |

---

## 4. Examples

```
$ID                     !ID FCI-FPGA 1 0
$GB                     !GB 12 256 384 0 0 -37 1
$SB 3 1                 !SB
$GB 3                   !GB 3 1
$ST 0 -1200             !ST
$AE                     !AE
$RV                     !RV 1 2 1 111 222 5255 1000 4000 7500
$RV                     !RV 0
$AD                     !AD
$ZZ                     !XX 0
$SB 1                   !XX 1
$ST 2 1                 !XX 1
```

## 5. Document history

| Version | Description | Author | Date |
|---|---|---|---|
| 1.0 | Initial release. | I. Morales | 2026/08/26 |
