-- Shared types/constants for the trigger_core design.
library ieee;
use ieee.std_logic_1164.all;

package trigger_core_pkg is

  -- Number of bits needed to represent the unsigned value max_val (e.g. clog2(4096) = 12,
  -- clog2(256) = 8). Used to size delay/depth/address fields from their generic bounds.
  function clog2(max_val : natural) return natural;

end package trigger_core_pkg;

package body trigger_core_pkg is

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

end package body trigger_core_pkg;
