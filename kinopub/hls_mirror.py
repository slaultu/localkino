"""Resumable HLS downloads: mirror the segments to disk, join them at the end.

ffmpeg cannot resume a playlist, so a shutdown, a crash or a dead CDN host
used to throw away everything downloaded so far. Here every 10-second segment
is its own file, written under a temporary name and renamed only once it is
complete - so after any interruption the finished segments are still there,
and at most one segment is fetched again. When all are on disk, ffmpeg joins
them locally into the mp4.

This module only understands the streams this service actually serves: plain
MPEG-TS segments, video and audio in separate playlists, no encryption, no
byte ranges. Anything else raises Unsupported and the caller falls back to
the old one-shot ffmpeg download.
"""
import re
import urllib.parse


class Unsupported(Exception):
    """A stream layout this mirror does not handle (encrypted, byte ranges...)."""


def _attr(attrs, name):
    match = re.search(r'(?:^|,)%s=("([^"]*)"|[^,]*)' % name, attrs)
    if not match:
        return None
    return match.group(2) if match.group(2) is not None else match.group(1)


def parse_master(text, url):
    """Variants (one per quality) and audio renditions, with absolute URLs."""
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    variants, audio = [], []
    for index, line in enumerate(lines):
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = line.split(":", 1)[1]
            uri = lines[index + 1] if index + 1 < len(lines) else ""
            resolution = _attr(attrs, "RESOLUTION") or ""
            width, _, height = resolution.partition("x")
            variants.append({
                "uri": urllib.parse.urljoin(url, uri),
                "bandwidth": int(_attr(attrs, "BANDWIDTH") or 0),
                "width": int(width) if width.isdigit() else 0,
                "height": int(height) if height.isdigit() else 0,
                "audio": _attr(attrs, "AUDIO"),
            })
        elif line.startswith("#EXT-X-MEDIA:") and _attr(line.split(":", 1)[1], "TYPE") == "AUDIO":
            attrs = line.split(":", 1)[1]
            uri = _attr(attrs, "URI")
            if uri:
                audio.append({
                    "uri": urllib.parse.urljoin(url, uri),
                    "group": _attr(attrs, "GROUP-ID"),
                    "name": _attr(attrs, "NAME") or "",
                    "default": (_attr(attrs, "DEFAULT") or "").upper() == "YES",
                })
    return {"variants": variants, "audio": audio}


def parse_media(text, url):
    """Segments of one media playlist; refuses layouts we cannot mirror."""
    if "#EXT-X-BYTERANGE" in text:
        raise Unsupported("byte-range segments")
    if "#EXT-X-MAP" in text:
        raise Unsupported("fMP4 init segment")
    key = re.search(r"#EXT-X-KEY:([^\n]*)", text)
    if key and (_attr(key.group(1), "METHOD") or "NONE").upper() != "NONE":
        raise Unsupported("encrypted segments")
    if "#EXT-X-ENDLIST" not in text:
        raise Unsupported("live or unfinished playlist")
    segments, duration = [], None
    target = 10
    for line in (l.strip() for l in text.splitlines()):
        if not line:
            continue
        if line.startswith("#EXT-X-TARGETDURATION:"):
            target = int(float(line.split(":", 1)[1] or 10))
        elif line.startswith("#EXTINF:"):
            duration = float(line.split(":", 1)[1].split(",", 1)[0] or 0)
        elif not line.startswith("#"):
            segments.append({"uri": urllib.parse.urljoin(url, line), "duration": duration or 0.0})
            duration = None
    if not segments:
        raise Unsupported("empty playlist")
    return {"segments": segments, "target": target}


def expected_width(quality):
    """'1080p' -> 1920. A 1920x800 widescreen stream is still a 1080p file."""
    digits = re.search(r"(\d{3,4})", str(quality or ""))
    if not digits:
        return 0
    return int(round(int(digits.group(1)) * 16 / 9.0))


def pick_variant(variants, quality):
    """The rendition whose width is closest to the quality the entry asked for."""
    if not variants:
        raise Unsupported("master playlist has no variants")
    wanted = expected_width(quality)
    if not wanted:
        return max(variants, key=lambda v: v["bandwidth"])
    return min(variants, key=lambda v: (abs((v["width"] or 0) - wanted), -v["bandwidth"]))


def pick_audio(renditions, group):
    """The default track of the variant's group, preferring AAC over AC3."""
    candidates = [r for r in renditions if r["group"] == group] if group else []
    if not candidates:
        return None
    ordered = sorted(candidates, key=lambda r: (not r["default"], "AC3" in r["name"].upper()))
    aac = [r for r in ordered if "AC3" not in r["name"].upper()]
    return (aac or ordered)[0]


def local_playlist(segments, filenames, target):
    """A media playlist pointing at the mirrored files, for ffmpeg to join."""
    out = ["#EXTM3U", "#EXT-X-VERSION:3", "#EXT-X-TARGETDURATION:%d" % max(1, int(target)),
           "#EXT-X-MEDIA-SEQUENCE:0", "#EXT-X-PLAYLIST-TYPE:VOD"]
    for segment, name in zip(segments, filenames):
        out.append("#EXTINF:%.3f," % (segment["duration"] or 0))
        out.append(name)
    out.append("#EXT-X-ENDLIST")
    return "\n".join(out) + "\n"
