"""Streaming FITS writer for a Capture's Raw Frames.

One :class:`FitsCube` owns one file. Frames are appended as they are pulled from
the Appsink rather than accumulated in memory: at 1456x1088 uint16 a frame is
3.2 MB, so a thousand-frame Capture would be 3.2 GB resident before a single
byte reached disk. The header is written once up front and its ``NAXIS3``
patched at :meth:`close`, which keeps a Capture that ends early — an operator
stop, or an aborting Drop — a conformant file rather than a truncated one.

Two conventions worth knowing before reading the code:

**Unsigned 16-bit.** Data is stored as ``BITPIX=16`` with ``BZERO=32768``, the
standard FITS convention for unsigned 16-bit, rather than assuming the camera's
bits sit in the low end of the word. Some GigE cameras left-align Mono12 into
the top of the 16-bit word, which would overflow a signed range. The acquisition
camera right-aligns (measured), but the convention is correct either way and
costs nothing.

**Timing lives in the extension, not the header.** The primary header holds one
``DATE-OBS``, describing frame zero — provisional when the file is opened,
before any frame exists, and rewritten at :meth:`FitsCube.close` from frame
zero's arrival. Per-frame times go in a ``FRAMETIME``
binary table, because the Appsink negotiates ``framerate=0/1`` — the device
free-runs, so there is no cadence to interpolate from. See ADR-0010.
"""

from __future__ import annotations

import warnings
from pathlib import Path

import numpy as np
from astropy.io import fits
from astropy.io.fits.verify import VerifyWarning

from app.clock import ClockReference, ClockState, utc_to_iso, utc_to_mjd

# Every FITS structural unit is a multiple of this many bytes.
BLOCK = 2880

# The FITS unsigned-16 offset: stored_int16 = unsigned_value - BZERO_U16.
BZERO_U16 = 32768


def frame_to_fits_bytes(data: bytes) -> bytes:
    """Convert one little-endian uint16 Raw Frame to FITS on-disk bytes.

    Two transformations, both required: FITS is big-endian, and ``BITPIX=16`` is
    *signed*, so each unsigned sample is shifted down by ``BZERO`` to land in
    the signed range. Readers add ``BZERO`` back and recover the original ADU.
    """
    samples = np.frombuffer(data, dtype="<u2")
    return (samples.astype(np.int32) - BZERO_U16).astype(">i2").tobytes()


def _set_card(header: fits.Header, keyword: str, value: object, comment: str) -> None:
    """Set a header card, dropping the comment rather than letting it truncate.

    A FITS card is 80 bytes. A medium-length string value — ``GSTCAPS`` is the
    one that bites — leaves no room for a comment but is not long enough to
    trigger the CONTINUE long-string convention, and astropy responds by
    silently truncating the comment with only a warning. Silent truncation in a
    provenance header is unacceptable: it produces files whose comments are
    subtly wrong, and warnings do not survive to whoever reads the file.

    So the comment is attempted, and dropped in full if it does not fit. The
    *value* is never sacrificed.

    The card is rendered here rather than at assignment because astropy formats
    lazily — assigning an over-long card succeeds silently and only warns later,
    when the whole header is serialized.
    """
    card = fits.Card(keyword, value, comment)
    with warnings.catch_warnings():
        warnings.simplefilter("error", VerifyWarning)
        try:
            card.image  # noqa: B018 - forces formatting, which is what may warn
        except VerifyWarning:
            card = fits.Card(keyword, value, "")
    header.append(card)


class FitsCube:
    """A FITS cube being written one frame at a time.

    :param path: File to create. Parent directories must already exist.
    :param width: Frame width in pixels (``NAXIS1``).
    :param height: Frame height in pixels (``NAXIS2``).
    :param expected: Frame count written into ``NAXIS3`` up front, corrected at
        :meth:`close` if the Capture ends early.
    :param header_cards: Ordered ``(keyword, value, comment)`` triples appended
        after the structural keywords — provenance, timing and clock state.
    """

    def __init__(
        self,
        path: Path,
        *,
        width: int,
        height: int,
        expected: int,
        header_cards: list[tuple[str, object, str]],
    ):
        self.path = path
        self._width = width
        self._height = height
        self._expected = expected
        self._header_cards = header_cards
        self._written = 0
        self._frame_bytes = width * height * 2

        self._times: list[tuple[int, int, float, bool]] = []
        self._first_utc_s: float | None = None
        self._cards_changed = False

        self._file = open(path, "wb")
        self._header_len = len(self._header_string(expected))
        self._file.write(self._header_string(expected))

    @property
    def written(self) -> int:
        """Number of frames appended so far."""
        return self._written

    @property
    def first_utc_s(self) -> float | None:
        """Unix epoch seconds of frame zero's arrival, or ``None`` before it."""
        return self._first_utc_s

    def update_cards(self, cards: list[tuple[str, object, str]]) -> None:
        """Replace the value and comment of existing header cards.

        Takes effect at :meth:`close`, which rewrites the header in place. Only
        cards already present may be updated: adding one could grow the header
        by a block and shear the data behind it.

        :raises KeyError: if a keyword is not already in the header.
        """
        index = {kw: i for i, (kw, _, _) in enumerate(self._header_cards) if kw != "COMMENT"}
        for keyword, value, comment in cards:
            if keyword not in index:
                raise KeyError(f"no {keyword} card to update")
            self._header_cards[index[keyword]] = (keyword, value, comment)
        self._cards_changed = True

    def _header_string(self, naxis3: int) -> bytes:
        """Render the primary header, padded to a whole number of FITS blocks.

        Structural keywords come first in the order the standard requires; the
        caller's provenance cards follow. Rendering is deterministic in length
        for any ``naxis3`` — a card is 80 bytes regardless of the number in it —
        which is what makes the in-place patch at :meth:`close` safe.
        """
        header = fits.Header()
        header["SIMPLE"] = (True, "conforms to FITS standard")
        header["BITPIX"] = (16, "16-bit signed integers, offset by BZERO")
        header["NAXIS"] = 3
        header["NAXIS1"] = (self._width, "frame width in pixels")
        header["NAXIS2"] = (self._height, "frame height in pixels")
        header["NAXIS3"] = (naxis3, "number of frames in this cube")
        header["EXTEND"] = (True, "FRAMETIME extension follows")
        header["BZERO"] = (BZERO_U16, "unsigned 16-bit convention")
        header["BSCALE"] = (1, "")
        for keyword, value, comment in self._header_cards:
            # COMMENT is a commentary keyword: each assignment appends another
            # card rather than replacing one, and it takes no separate comment
            # field, so it cannot be set through the (value, comment) form.
            if keyword == "COMMENT":
                header["COMMENT"] = value
            else:
                _set_card(header, keyword, value, comment)
        return header.tostring(sep="", endcard=True, padding=True).encode("ascii")

    def append(self, data: bytes, *, pts_ns: int, utc_s: float, dropped_before: bool) -> None:
        """Append one frame and record its timing row.

        :param data: Raw little-endian uint16 buffer, exactly one frame.
        :param pts_ns: The buffer's PTS — the raw measurement, retained verbatim.
        :param utc_s: Derived Unix epoch seconds for this frame's arrival.
        :param dropped_before: Whether a gap was detected ahead of this frame.
        :raises ValueError: if ``data`` is not exactly one frame's worth of bytes,
            which would silently shear every subsequent frame in the cube.
        """
        if len(data) != self._frame_bytes:
            raise ValueError(
                f"frame is {len(data)} bytes, expected {self._frame_bytes} "
                f"({self._width}x{self._height} uint16)"
            )
        self._file.write(frame_to_fits_bytes(data))
        if self._first_utc_s is None:
            self._first_utc_s = utc_s
        self._times.append((self._written, pts_ns, utc_to_mjd(utc_s), dropped_before))
        self._written += 1

    def close(self) -> int:
        """Finish the file and return the frame count actually written.

        Pads the data segment to a block boundary, rewrites the header in place
        if ``NAXIS3`` changed (the Capture ended early) or cards were updated
        (:meth:`update_cards`), then appends the ``FRAMETIME`` extension. Safe to
        call twice; the second call is a no-op.
        """
        if self._file.closed:
            return self._written

        remainder = (self._written * self._frame_bytes) % BLOCK
        if remainder:
            self._file.write(b"\0" * (BLOCK - remainder))

        if self._written != self._expected or self._cards_changed:
            patched = self._header_string(self._written)
            # A card is 80 bytes whatever value it holds, so the patched header
            # occupies the same blocks as the original. Asserted rather than
            # assumed: writing a different length here would shear the data.
            if len(patched) != self._header_len:
                raise RuntimeError(
                    f"patched header changed length ({self._header_len} -> {len(patched)}); "
                    "refusing to corrupt the cube"
                )
            self._file.seek(0)
            self._file.write(patched)

        self._file.close()
        self._append_frametime()
        return self._written

    def _append_frametime(self) -> None:
        """Append the per-frame timing table as a binary table extension."""
        if not self._times:
            return
        index, pts, mjd, dropped = zip(*self._times)
        table = fits.BinTableHDU.from_columns(
            [
                fits.Column(name="FRAME", format="J", array=np.array(index, dtype=np.int32)),
                fits.Column(
                    name="PTS", format="K", unit="ns", array=np.array(pts, dtype=np.int64)
                ),
                fits.Column(name="MJD", format="D", unit="d", array=np.array(mjd)),
                fits.Column(name="DROP", format="L", array=np.array(dropped, dtype=bool)),
            ],
            name="FRAMETIME",
        )
        table.header["COMMENT"] = "PTS is the raw GStreamer buffer timestamp (pipeline"
        table.header["COMMENT"] = "running time). MJD is derived from it via CLKREF/PTSREF"
        table.header["COMMENT"] = "in the primary header and is frame ARRIVAL, not exposure"
        table.header["COMMENT"] = "start. DROP marks a gap detected before this frame."
        fits.append(self.path, table.data, table.header)

    def __enter__(self) -> "FitsCube":
        return self

    def __exit__(self, *_exc) -> None:
        self.close()


def date_obs_cards(arrival_utc_s: float, exposure_us: float | None) -> list[tuple[str, object, str]]:
    """``DATE-OBS``/``MJD-OBS`` for a frame that arrived at ``arrival_utc_s``.

    The estimated exposure start: arrival minus the exposure time (ADR-0010).
    One formula, used twice — for the provisional value written when the file
    opens, and for the real one :class:`~app.capture.CaptureManager` sets from
    frame zero before closing.
    """
    date_obs_s = arrival_utc_s - (exposure_us or 0.0) / 1e6
    return [
        ("DATE-OBS", utc_to_iso(date_obs_s), "estimated exposure start, frame 0"),
        ("MJD-OBS", utc_to_mjd(date_obs_s), "MJD of DATE-OBS"),
    ]


def provenance_cards(
    *,
    camera_id: str,
    label: str,
    device: str | None,
    pixel_format: str,
    bit_depth: int,
    caps: str,
    exposure_us: float | None,
    gain: float | None,
    provisional_utc_s: float,
    reference: ClockReference,
    state: ClockState,
    capture_id: str,
    capture_label: str | None,
    version: str,
) -> list[tuple[str, object, str]]:
    """Build the primary header's provenance cards.

    ``DATE-OBS`` is the *estimated* exposure start of the first frame — arrival
    minus the exposure time — and carries an unmodelled transport-latency
    systematic. No frame exists when the header is first written, so it is
    computed here from ``provisional_utc_s`` (when the Capture was commanded) and
    rewritten from frame zero's actual arrival before the file closes; it stays
    provisional only in a file with no frames. ``CLOCKSYN``/``TIMEERR`` record whether the host clock was
    disciplined at all, so a file written on an unsynchronized host is
    distinguishable from a correct one years later without anyone remembering.
    ``CLKREF``/``PTSREF`` are the sampled offset pair, retained so every
    timestamp can be re-derived from the raw PTS. All four are ADR-0010.
    """
    exposure_s = (exposure_us or 0.0) / 1e6

    cards: list[tuple[str, object, str]] = [
        *date_obs_cards(provisional_utc_s, exposure_us),
        ("TIMESYS", "UTC", "time scale for DATE-OBS/MJD-OBS"),
        ("EXPTIME", exposure_s, "[s] exposure time"),
    ]
    if gain is not None:
        cards.append(("GAIN", gain, "camera gain, native units"))
    cards += [
        ("INSTRUME", label, "camera label"),
        ("CAMERA", camera_id, "gstvmb instance id"),
        ("DEVICE", device or "", "GenICam device id"),
        ("PIXFMT", pixel_format, "declared GenICam pixel format"),
        ("BITDEPTH", bit_depth, "significant bits per pixel"),
        ("GSTCAPS", caps, "negotiated GStreamer caps"),
        ("CLOCKSYN", state.synchronized, "host clock disciplined by NTP"),
        ("TIMEERR", state.max_error_s, "[s] kernel estimated clock error bound"),
        ("CLKREF", utc_to_iso(reference.utc_s), "UTC half of the PTS->UTC offset pair"),
        ("PTSREF", reference.clock_ns, "[ns] pipeline clock at CLKREF"),
        ("PTSBASE", reference.base_time_ns, "[ns] pipeline base time"),
        ("CAPTID", capture_id, "capture directory name"),
        ("LABEL", capture_label or "", "operator label"),
        ("CREATOR", f"gstvmb {version}", "software that wrote this file"),
    ]

    comments = [
        "DATE-OBS is ESTIMATED as frame arrival minus EXPTIME. The camera",
        "surfaces no exposure timestamp through vmbsrc, so exposure, readout",
        "and variable GigE transport latency are unmodelled. Relative times",
        "between frames (see the FRAMETIME extension) are sub-millisecond;",
        "the absolute epoch is only as good as the host clock -- consult",
        "CLOCKSYN and TIMEERR before trusting it. See ADR-0010.",
    ]
    if not state.available:
        comments.append("CLOCKSYN/TIMEERR unavailable on this platform; values are placeholders.")
    elif not state.error_is_bounded:
        comments.append("TIMEERR is pegged at the kernel ceiling: the error is UNBOUNDED.")
    cards += [("COMMENT", line, "") for line in comments]
    return cards
