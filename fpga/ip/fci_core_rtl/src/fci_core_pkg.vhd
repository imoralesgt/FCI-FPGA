-- Shared helpers for the VHDL fci_core. Separate package from the other cores' packages: these
-- are independently packaged IP and must not depend on a common library.
library ieee;
use ieee.std_logic_1164.all;
use ieee.numeric_std.all;

package fci_core_pkg is

  function clog2(max_val : natural) return natural;

  -- Reverses the low `width` bits of v. The FFT is configured for BIT-REVERSED output ordering,
  -- which means beat n of the output stream carries bin bit_reverse(n) rather than bin n.
  --
  -- Taking bit-reversed output is deliberate: natural ordering costs the FFT IP a reorder buffer
  -- (~1 RAMB36 for 1024-point 16-bit complex) purely to permute the stream, and on a device at 81%
  -- BRAM that is a real price. Reversing the index here instead is pure wire reversal -- no logic,
  -- no latency, no memory -- and firmware never sees the difference: the window bounds it programs
  -- stay ordinary bin numbers.
  function bit_reverse(v : unsigned; width : natural) return unsigned;

end package fci_core_pkg;

package body fci_core_pkg is

  function clog2(max_val : natural) return natural is
    variable bits : natural := 0;
    variable v    : natural := max_val;
  begin
    while v > 0 loop
      bits := bits + 1;
      v    := v / 2;
    end loop;
    if bits = 0 then
      bits := 1;
    end if;
    return bits;
  end function clog2;

  function bit_reverse(v : unsigned; width : natural) return unsigned is
    variable r : unsigned(width - 1 downto 0);
  begin
    for i in 0 to width - 1 loop
      r(i) := v(width - 1 - i);
    end loop;
    return r;
  end function bit_reverse;

end package body fci_core_pkg;
