from bs4 import BeautifulSoup, Tag

import waha_parse_utils as utils


def run(page, section_anchor):
    """
    Extract one Army Rules section from the current Wahapedia faction page.

    Example:
      run(page, "Army-Rules")
      run(page, "Army-Rules-2")

    Returns:
      [
        {
          "name": "Oath of Moment",
          "content": [...],
          "heading_class": [...]
        }
      ]
    """

    soup = BeautifulSoup(page.content(), "html.parser")

    anchor = soup.find("a", attrs={"name": section_anchor})
    if anchor is None:
        raise Exception(f"Army Rules section '{section_anchor}' not found.")

    container = _find_army_rules_container(anchor)
    if container is None:
        raise Exception(
            f"Could not locate Army Rules content for '{section_anchor}'."
        )

    return _extract_rule_cards(container)


def _find_army_rules_container(anchor):
    """
    Army Rules sections look like:

      <a name="Army-Rules"></a>
      <h2 class="outline_header">Army Rules</h2>
      <div class="Columns2">...</div>

    or:

      <a name="Army-Rules-2"></a>
      <h3 class="outline_header">Army Rules</h3>
      <div class="Columns2">...</div>
    """

    for node in anchor.next_siblings:
        if not isinstance(node, Tag):
            continue

        if (
            node.name in ("h2", "h3")
            and "outline_header" in node.get("class", [])
        ):
            continue

        if node.name == "div" and (
            "Columns2" in node.get("class", [])
            or "BreakInsideAvoid" in node.get("class", [])
        ):
            return node

        if node.name in ("h2", "h3"):
            break

    return None



def _is_rule_heading(node):
    return (
        isinstance(node, Tag)
        and node.name in ("h2", "h3", "h4")
        and "outline_header" not in node.get("class", [])
        and not node.find_parent(class_="str10Wrap")
    )


def _contains_rule_heading(node):
    if not isinstance(node, Tag):
        return False
    return any(_is_rule_heading(h) for h in node.find_all(["h2", "h3", "h4"]))


def _extract_rule_cards(container):
    rules = []

    headings = [
        h for h in container.find_all(["h2", "h3", "h4"])
        if not (
            "outline_header" in h.get("class", [])
            or h.find_parent(class_="str10Wrap")
        )
    ]

    for heading in headings:
        content_nodes = []

        for node in heading.next_siblings:
            if _is_rule_heading(node) or _contains_rule_heading(node):
                break

            if utils.is_ignorable_node(node) and not utils.is_br(node):
                continue

            content_nodes.append(node)

        content = utils.extract_content_blocks(content_nodes)

        if not content:
            continue

        rules.append({
            "name": utils.clean_text(heading),
            "content": content,
            "heading_class": heading.get("class", []),
        })

    return rules