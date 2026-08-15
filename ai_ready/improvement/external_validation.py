"""Non-circular external validation metrics.

These metrics verify improvement using INDEPENDENT code paths from the 10
signal collectors. They use raw file I/O and regex only — no imports from
``ai_ready.rules`` — so they don't share blind spots with the collectors.

Four metrics:
1. Link Integrity (Independent) — raw regex link extraction, filesystem existence check
2. Heading Structure (Independent) — raw regex heading count, duplicate detection, hierarchy
3. Content Coverage — content length change, front-matter key presence
4. Orphan Resolution (Independent) — raw grep for inbound links from other files

All metrics are deterministic (no LLM, no embeddings, no collector imports).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any


def _extract_links_regex(content: str) -> list[tuple[str, str]]:
    """Extract all [text](url) links from markdown content via raw regex.

    Returns a list of (link_text, link_target) tuples.
    Excludes image links ![alt](url) and code spans.
    """
    # Remove code blocks (don't extract links from code)
    cleaned = re.sub(r"```[\s\S]*?```", "", content)
    cleaned = re.sub(r"`[^`]+`", "", cleaned)
    # Remove image links
    cleaned = re.sub(r"!\[[^\]]*\]\([^)]+\)", "", cleaned)
    # Extract markdown links
    links = re.findall(r"\[([^\]]+)\]\(([^)]+)\)", cleaned)
    return links


def _extract_headings_regex(content: str) -> list[tuple[int, str]]:
    """Extract all headings from markdown content via raw regex.

    Returns a list of (level, text) tuples.
    """
    headings = []
    for match in re.finditer(r"^(#{1,6})\s+(.+)$", content, re.MULTILINE):
        level = len(match.group(1))
        text = match.group(2).strip()
        headings.append((level, text))
    return headings


def _has_front_matter(content: str) -> dict[str, str]:
    """Extract YAML front-matter keys from markdown content.

    Returns a dict of key -> value string for any YAML front-matter block.
    """
    front_matter = {}
    match = re.match(r"^---\s*\n(.*?)\n---\s*\n", content, re.DOTALL)
    if match:
        for line in match.group(1).split("\n"):
            kv = re.match(r"^(\w+):\s*(.*)$", line)
            if kv:
                front_matter[kv.group(1)] = kv.group(2).strip()
    return front_matter


def _count_inbound_links(target_uri: str, all_files: dict[str, str]) -> int:
    """Count how many OTHER files contain a link to target_uri via raw regex.

    Args:
        target_uri: The URI to search for links to.
        all_files: Dict of {uri: content} for all files in the KB.

    Returns:
        Number of files that link to target_uri (excluding self-links).
    """
    # Normalize target — links may use relative paths, so check both
    # the full URI and the filename portion
    target_filename = target_uri.rsplit("/", 1)[-1] if "/" in target_uri else target_uri

    inbound = 0
    for uri, content in all_files.items():
        if uri == target_uri:
            continue  # don't count self-links
        links = _extract_links_regex(content)
        for _, link_target in links:
            if link_target == target_uri or link_target.endswith(target_filename):
                inbound += 1
                break  # count each file once
    return inbound


def validate_improvement(
    before_artifacts: dict[str, str],
    after_artifacts: dict[str, str],
    modified_uris: list[str],
) -> dict[str, Any]:
    """Run 4 independent validation metrics on modified artifacts.

    Args:
        before_artifacts: Dict of {uri: content} for artifacts before modification.
        after_artifacts: Dict of {uri: content} for artifacts after modification.
        modified_uris: List of URIs that were modified by the executor.

    Returns:
        Dict with 4 metric sections, each containing before/after counts
        and a delta indicating improvement or regression.
    """
    results: dict[str, Any] = {}

    # --- Metric 1: Link Integrity (Independent) ---
    # Raw regex link extraction + filesystem existence check
    # Compares broken link count before vs after
    link_before = {"total_links": 0, "broken_links": 0, "broken_details": []}
    link_after = {"total_links": 0, "broken_links": 0, "broken_details": []}

    all_uris = set(before_artifacts.keys()) | set(after_artifacts.keys())

    for label, artifacts, stats in [
        ("before", before_artifacts, link_before),
        ("after", after_artifacts, link_after),
    ]:
        for uri, content in artifacts.items():
            links = _extract_links_regex(content)
            stats["total_links"] += len(links)
            for link_text, link_target in links:
                # Check if link target exists in the artifact set
                # Normalize: remove leading ./ and #
                normalized = link_target.lstrip("./").split("#")[0].split("?")[0]
                if not normalized:
                    continue
                if normalized.startswith("http"):
                    continue  # external link, skip
                # Check if target exists in the artifact set
                target_found = False
                for check_uri in all_uris:
                    if check_uri == normalized or check_uri.endswith(normalized):
                        target_found = True
                        break
                if not target_found:
                    stats["broken_links"] += 1
                    stats["broken_details"].append({
                        "source": uri,
                        "target": link_target,
                    })

    results["link_integrity"] = {
        "before_total_links": link_before["total_links"],
        "before_broken_links": link_before["broken_links"],
        "after_total_links": link_after["total_links"],
        "after_broken_links": link_after["broken_links"],
        "broken_delta": link_after["broken_links"] - link_before["broken_links"],
        "improved": link_after["broken_links"] < link_before["broken_links"],
    }

    # --- Metric 2: Heading Structure (Independent) ---
    # Raw regex heading count, duplicate detection, hierarchy check
    heading_before = {"total_headings": 0, "duplicates": 0, "hierarchy_issues": 0}
    heading_after = {"total_headings": 0, "duplicates": 0, "hierarchy_issues": 0}

    for label, artifacts, stats in [
        ("before", before_artifacts, heading_before),
        ("after", after_artifacts, heading_after),
    ]:
        for uri, content in artifacts.items():
            if uri not in modified_uris:
                continue
            headings = _extract_headings_regex(content)
            stats["total_headings"] += len(headings)
            # Check for duplicate headings
            seen = set()
            for level, text in headings:
                if text in seen:
                    stats["duplicates"] += 1
                seen.add(text)
            # Check hierarchy: no H3 without preceding H2, no H2 without H1
            prev_level = 0
            for level, text in headings:
                if level > prev_level + 1 and prev_level > 0:
                    stats["hierarchy_issues"] += 1
                prev_level = level

    results["heading_structure"] = {
        "before_total_headings": heading_before["total_headings"],
        "before_duplicates": heading_before["duplicates"],
        "before_hierarchy_issues": heading_before["hierarchy_issues"],
        "after_total_headings": heading_after["total_headings"],
        "after_duplicates": heading_after["duplicates"],
        "after_hierarchy_issues": heading_after["hierarchy_issues"],
        "duplicates_delta": heading_after["duplicates"] - heading_before["duplicates"],
        "hierarchy_delta": heading_after["hierarchy_issues"] - heading_before["hierarchy_issues"],
        "improved": (
            heading_after["duplicates"] < heading_before["duplicates"]
            or heading_after["hierarchy_issues"] < heading_before["hierarchy_issues"]
        ),
    }

    # --- Metric 3: Content Coverage ---
    # Check if content actually changed for modified URIs
    # and if metadata was supposed to be added (front-matter keys)
    coverage = {"modified_files": 0, "content_changed": 0, "metadata_added": 0, "unchanged": 0}

    for uri in modified_uris:
        before_content = before_artifacts.get(uri, "")
        after_content = after_artifacts.get(uri, "")
        coverage["modified_files"] += 1
        if before_content != after_content:
            coverage["content_changed"] += 1
        else:
            coverage["unchanged"] += 1

        # Check if front-matter keys were added
        before_fm = _has_front_matter(before_content)
        after_fm = _has_front_matter(after_content)
        new_keys = set(after_fm.keys()) - set(before_fm.keys())
        if new_keys:
            coverage["metadata_added"] += 1

    results["content_coverage"] = {
        "modified_files": coverage["modified_files"],
        "content_changed": coverage["content_changed"],
        "content_unchanged": coverage["unchanged"],
        "metadata_keys_added": coverage["metadata_added"],
        "all_changed": coverage["content_changed"] == coverage["modified_files"] if coverage["modified_files"] > 0 else True,
    }

    # --- Metric 4: Orphan Resolution (Independent) ---
    # Raw grep for inbound links from OTHER files
    # If orphan was supposed to be resolved → verify inbound link count increased
    orphan_before = {"total_inbound": 0, "orphans": 0}
    orphan_after = {"total_inbound": 0, "orphans": 0}

    for label, artifacts, stats in [
        ("before", before_artifacts, orphan_before),
        ("after", after_artifacts, orphan_after),
    ]:
        for uri, content in artifacts.items():
            inbound = _count_inbound_links(uri, artifacts)
            stats["total_inbound"] += inbound
            if inbound == 0:
                stats["orphans"] += 1

    results["orphan_resolution"] = {
        "before_total_inbound": orphan_before["total_inbound"],
        "before_orphans": orphan_before["orphans"],
        "after_total_inbound": orphan_after["total_inbound"],
        "after_orphans": orphan_after["orphans"],
        "orphans_delta": orphan_after["orphans"] - orphan_before["orphans"],
        "inbound_delta": orphan_after["total_inbound"] - orphan_before["total_inbound"],
        "improved": orphan_after["orphans"] < orphan_before["orphans"],
    }

    # --- Overall external validation summary ---
    metrics_improved = sum(1 for m in results.values() if m.get("improved", False))
    metrics_regressed = sum(
        1 for m in results.values()
        if "broken_delta" in m and m.get("broken_delta", 0) > 0
        or "duplicates_delta" in m and m.get("duplicates_delta", 0) > 0
        or "orphans_delta" in m and m.get("orphans_delta", 0) > 0
    )
    results["summary"] = {
        "metrics_improved": metrics_improved,
        "metrics_regressed": metrics_regressed,
        "overall": "improved" if metrics_improved > metrics_regressed else "degraded" if metrics_regressed > metrics_improved else "neutral",
    }

    return results
