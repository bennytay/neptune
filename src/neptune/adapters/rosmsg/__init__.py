"""ROS message definitions and payload decoding that the stream adapters share (ADR 0068 §1).

This is not an adapter. MCAP, rosbag1 and rosbag2 all carry ROS messages: definitions in
``ros1msg``, ``ros2msg`` or ``ros2idl`` text and payloads in ROS 1's serialisation or ROS 2's
CDR. Adapters never import each other (ADR 0008 §4); each imports this, which, like
``neptune.adapters.structured`` (ADR 0055), imports only the model and the contract.

- ``definitions``: the declared definition parsed into types, bounded, never completed from a
  bundled copy.
- ``codec``: a layout compiled from a definition (columns named by field path) and the decoder
  that reads one payload into it, bounded against hostile counts and lengths.
- ``streams``: the per-stream decision (decoded fully, partly, header only, or not, and why), the
  config options every stream adapter exposes, and one row's cells.

Nothing here knows a record kind or a finding code; the adapter reports in its own name.
"""
