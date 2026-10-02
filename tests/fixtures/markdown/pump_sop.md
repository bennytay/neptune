---
title: Pump room start-up SOP
site: north-plant
revision: 3
---

# Pump room start-up

Close valve **V-12** before entering the pump room.
Check that the pressure gauge reads below 2 bar.

> Warning: hearing protection is required
near pump P-2.

## Procedure

1. Isolate the pump at the local panel.
2. Start pump P-2.
   - Confirm discharge pressure.
   - Log the reading in the [site register](https://example.invalid/register).

     Repeat after five minutes.
3. Open valve V-12 slowly.

```bash
ros2 run pump_monitor log --pump P-2
```

## Torque table

| Bolt | Torque | Unit |
|------|-------:|------|
| M8   | 25     | N·m  |
| M10  |        | N·m  |
| M12  | 85 \| 90 | N·m |

Sign-off
--------

<!-- reviewed by operations -->

[register]: https://example.invalid/register
