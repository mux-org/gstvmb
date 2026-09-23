# Context: gstvmb (Allied Vision camera service)

One containerized FastAPI service fronting **one** Allied Vision (VimbaX/GenICam)
camera through GStreamer. It owns the camera's GStreamer pipeline, exposes a
small hand-written REST surface for the controls an operator actually needs, and
is the only process permitted to open the device.

## Language

### Instance, Camera, Device

**Instance**:
One running container of this image, bound to exactly one Camera by one mounted
YAML config file. Scaling means more Instances, never more workers — a second
worker would open the same Device twice.
_Avoid_: server, node, worker

**Camera**:
The logical camera an Instance serves, identified by the operator-assigned `id`
(e.g. `acquisition`) that also names its MediaMTX path and its proxy routes.
One Camera per Instance.
_Avoid_: cam, source, sensor

**Device**:
The physical camera, named by its GenICam device id (e.g. `DEV_000A472C98F3`).
Bound in exactly one place — the `camera=` assignment inside the Pipeline
description — and parsed back out of that string to report it. Never configured
twice.
_Avoid_: camera id, serial, hardware

### Pipeline

**Pipeline**:
The gst-launch description this Instance was configured with, plus the lifecycle
that drives it. Read once at process start; editing the config requires
restarting the container, not restarting the Pipeline.
_Avoid_: stream, graph

**Pipeline state**:
One of `idle` (never started since boot), `playing`, `stopped` (an operator
deliberately stopped it), `error`. It encodes operator *intent*, not just
liveness — which is what lets a UI auto-start a fresh deployment while never
reversing a deliberate stop.
_Avoid_: status, running

**Control**:
A single camera property reachable over REST through a hand-written route
(exposure time, gain, auto-exposure, white balance, ROI). Properties without a
route are unreachable by design; that absence is the safety boundary keeping
advanced knobs — trigger configuration in particular — off the API.
_Avoid_: setting, parameter, feature

**Region of interest (ROI)**:
Width, height and both offsets treated as **one** resource, because they are
interdependent and setting them separately can be refused mid-sequence. Always
replaced as a whole.
_Avoid_: crop, window, subframe

### Frames

**Appsink**:
A named sink element in the Pipeline where frames leave GStreamer and become
reachable over REST. Named in the Pipeline description; addressed by that name.
It is a **queue, not a tap**: it holds a backlog and hands out the *oldest*
frame first, so anything that means "what is the camera showing now" must
discard the backlog before reading. An unbounded backlog is also an unbounded
memory leak, so this service never allows one.
_Avoid_: sink, endpoint, tap (a tap implies the present; this is a queue)

**Raw Frame**:
One buffer pulled from an Appsink that has been kept out of the display path
entirely — no `videoconvert`, no encode, no 8-bit reduction. On the acquisition
Camera this is `video/x-raw,format=GRAY16_LE`, 1456×1088, **right-aligned** —
the stored 16-bit words are sensor ADU directly, not ADU scaled into the top of
the word. The 16-bit word is a *container*: the Device currently fills only 10
of those bits, and nothing in the Pipeline can tell you so.
_Avoid_: raw bytes, raw data, image (all three are used loosely for three
different things — see Flagged ambiguities)

**Pixel format**:
The GenICam format the Device is producing — measured as `Mono10` on the
acquisition Camera, despite ADR-0002 assuming 12-bit. It is **declared, never discovered**: `GRAY16_LE` is a container, not a
depth, and Mono10/12/14/16 negotiate identically, so no Instance can determine
by inspection whether its values top out at 1023, 4095, 16383 or 65535. Like the
Device id, it is a hardware fact pinned in the one config file. Bit depth is
derived from it, never configured alongside it.
_Avoid_: bit depth (that is derived), resolution (means pixel count here),
GRAY16_LE (that is the GStreamer container, not the format)

**Display Frame**:
A frame on the encoding path: reduced to 8 bits and H.264-encoded for the live
WebRTC view. Display-grade, never photometric (ADR-0002).
_Avoid_: preview, stream frame

### Capture

**Capture**:
One commanded run that persists Raw Frames from this Instance to the archive.
At most one per Instance at a time — one Device, one Pipeline, one Appsink, so a
second concurrent Capture would interleave pulls from the same sink and corrupt
both. Commanded and polled like the Pipeline, never requested and waited on.
_Avoid_: recording, acquisition (means the Camera named `acquisition` here),
save, export

**Extent**:
What bounds a Capture. **Count** (a fixed number of frames, of which Snapshot is
the count-1 case) is bounded by frames and can promise completeness; Duration
and Until-stopped are bounded by time or not at all, and can only report what
arrived.
_Avoid_: length, limit, duration (that is one specific extent)

**Drop**:
A frame the Device produced that never reached the Appsink — most often an
incomplete GigE transmission, which `vmbsrc` discards silently by default.
Detectable only as a discontinuity in the PTS cadence. A Drop **aborts** a
Count Capture, because "50 frames" and "50 consecutive frames" are different
claims and only the second is worth keeping.
_Avoid_: dropped packet (that is the transport-level cause), miss, gap

**Frame time**:
The UTC instant a Raw Frame is stamped with, derived from its buffer PTS against
a wall-clock offset sampled once at Capture start. It is the *estimated exposure
start* — arrival minus exposure time — never a measured one, because the Device
surfaces no camera-side timestamp through `vmbsrc`. Relative times within a
Capture are sub-millisecond; the absolute epoch is only as good as the host
clock, which is why every file records that clock's own error bound (ADR-0010).
_Avoid_: exposure time (that is the Control), timestamp (unqualified — say
which), DATE-OBS (that is the header card, not the concept)

## Flagged ambiguities

**"raw"** is used for at least three different things and needs qualifying every
time:
1. **Raw Frame** — unencoded, full-precision sensor data (the sense that matters
   for saved science data).
2. **raw bytes** — the HTTP transport encoding of a pulled buffer
   (`application/octet-stream`), which says nothing about precision. A Display
   Frame can be delivered as raw bytes.
3. **`video/x-raw`** — a GStreamer caps media type meaning "not compressed",
   which includes 8-bit `I420`.

Prefer **Raw Frame** for the first and spell the others out. "It goes to the raw
appsink" is ambiguous; "it carries Raw Frames" is not.

**"Timestamp" is never sufficient on its own.** At least four distinct times are
in play: the buffer PTS (pipeline running time at arrival), the derived UTC
arrival, the estimated exposure start written as `DATE-OBS`, and the Capture's
own start time used to name its directory. Say which.

**Frame rate is not knowable from caps.** The Appsink negotiates
`framerate=0/1` — unknown/variable — because the Device free-runs. Any timing
record has to be per-frame; nothing can be derived from the Pipeline's caps.

## Example dialogue

> **Dev:** The acquisition pipeline already produces frames — can't the capture
> just read those?
>
> **Operator:** Not the ones going to the browser. Those are Display Frames —
> they've been through `videoconvert` and an 8-bit encode, so the faint end is
> already gone. I need Raw Frames.
>
> **Dev:** So the Appsink has to sit upstream of `videoconvert`, straight off
> `vmbsrc`.
>
> **Operator:** Right. And pin the format in the Pipeline description, don't
> inherit whatever the Device happens to be set to — I need to know what a file
> contains without asking the camera what mood it was in.
>
> **Dev:** The Device id is pinned there too, in `camera=`. Same principle.
>
> **Operator:** Same principle. One place.
>
> **Dev:** What about the numbers themselves — are they 0–4095?
>
> **Operator:** Right-aligned, so yes, they're ADU. If they'd been left-aligned
> they'd all be multiples of 16 and every value would be sixteen times too big.
> That distinction has to survive into whatever we write out, or the file is
> uninterpretable later.
