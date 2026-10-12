"""Minimal, dependency-free SDF record parsing and integrity checks.

Used for vendor-tool output (HYDE/FlexX), where we need every data tag with
its exact vendor name and must not lose records to RDKit sanitisation
failures on unusual valences.
"""
from __future__ import annotations

import dataclasses
import re

_TAG_RE = re.compile(r"^>\s*<([^>]+)>")


class SdfError(ValueError):
    pass


@dataclasses.dataclass
class SdfRecord:
    name: str
    molblock: str            # everything up to and including "M  END"
    props: dict[str, str]
    text: str                # the full record including "$$$$"


def parse_sdf(text: str) -> list[SdfRecord]:
    """Split an SDF into records. Raises SdfError if the file is truncated."""
    if not text.strip():
        return []
    chunks = text.split("$$$$")
    tail = chunks[-1].strip()
    if tail:
        raise SdfError("SDF is truncated: the final record has no '$$$$' terminator")
    records = []
    for chunk in chunks[:-1]:
        chunk = chunk.lstrip("\r\n")
        lines = chunk.splitlines()
        if "M  END" not in chunk:
            raise SdfError("SDF record has no 'M  END' line")
        name = lines[0].strip() if lines else ""
        end = next(i for i, ln in enumerate(lines) if ln.startswith("M  END"))
        props: dict[str, str] = {}
        i = end + 1
        while i < len(lines):
            m = _TAG_RE.match(lines[i])
            if m:
                j = i + 1
                body = []
                while j < len(lines) and lines[j].strip() != "":
                    body.append(lines[j])
                    j += 1
                props[m.group(1)] = "\n".join(body)
                i = j
            i += 1
        records.append(
            SdfRecord(name, "\n".join(lines[: end + 1]) + "\n", props, chunk.rstrip("\n") + "\n$$$$\n")
        )
    return records


def format_tag(name: str, value) -> str:
    return f">  <{name}>\n{str(value).replace(chr(10), ' ')}\n\n"


def with_props(molblock: str, props: dict) -> str:
    """Render a record: molblock + data tags + terminator."""
    body = molblock if molblock.endswith("\n") else molblock + "\n"
    for k, v in props.items():
        if v is not None:
            body += format_tag(k, v)
    return body + "$$$$\n"
