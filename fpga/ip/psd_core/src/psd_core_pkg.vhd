-- Shared helpers for the psd_core design. Separate package from trigger_core_pkg/blr_core_pkg:
-- these are independently packaged IP cores and must not share a library.
library ieee;
use ieee.std_logic_1164.all;

package psd_core_pkg is
  function clog2(max_val : natural) return natural;
end package psd_core_pkg;

package body psd_core_pkg is
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
end package body psd_core_pkg;
