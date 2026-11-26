"""Reads the hidden fields of the ASP.NET WebForms tracking form of tpcindia.com.

The page is untrusted input: it is parsed as data only; nothing in it is executed, followed
or interpreted as an instruction.
"""

from __future__ import annotations

from bs4 import BeautifulSoup

from courier_adapter_sdk import SchemaDrift


def hidden_fields(html: str) -> dict[str, str]:
    """Every hidden input of the page's form, by name and value.

    ASP.NET WebForms guards a postback with hidden fields (__VIEWSTATE and its companions)
    that must be sent back unchanged. A page without __VIEWSTATE is not the form this
    adapter knows, so it is reported as schema drift.
    """
    soup = BeautifulSoup(html, "html.parser")
    fields: dict[str, str] = {}
    for field in soup.find_all("input", attrs={"type": "hidden"}):
        name = field.get("name")
        if not isinstance(name, str) or not name:
            continue
        value = field.get("value")
        fields[name] = value if isinstance(value, str) else ""
    if "__VIEWSTATE" not in fields:
        raise SchemaDrift("the tracking form has no __VIEWSTATE")
    return fields
