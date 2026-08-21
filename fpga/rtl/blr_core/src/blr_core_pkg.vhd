-- Shared helpers for the blr_core design. Deliberately a separate package from
-- trigger_core_pkg: these are independently packaged IP cores and must not share a library.
library ieee;
use ieee.std_logic_1164.all;

package blr_core_pkg is

  -- Number of bits needed to represent the unsigned value max_val (e.g. clog2(16) = 5,
  -- clog2(15) = 4). Used to size register fields from their generic bounds.
  function clog2(max_val : natural) return natural;

end package blr_core_pkg;

package body blr_core_pkg is

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

end package body blr_core_pkg;
