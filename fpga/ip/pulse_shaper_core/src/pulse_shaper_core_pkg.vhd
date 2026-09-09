-- Shared helpers for the pulse_shaper_core design. Separate package from trigger_core_pkg/
-- blr_core_pkg/psd_core_pkg/fci_core_rtl's own package: these are independently packaged IP cores
-- and must not share a library (see psd_core_pkg.vhd's identical note).
library ieee;
use ieee.std_logic_1164.all;

package pulse_shaper_core_pkg is
  function clog2(max_val : natural) return natural;
end package pulse_shaper_core_pkg;

package body pulse_shaper_core_pkg is
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
end package body pulse_shaper_core_pkg;
