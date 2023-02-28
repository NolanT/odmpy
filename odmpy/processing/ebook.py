# Copyright (C) 2023 github.com/ping
#
# This file is part of odmpy.
#
# odmpy is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License as published by
# the Free Software Foundation, either version 3 of the License, or
# (at your option) any later version.
#
# odmpy is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with odmpy.  If not, see <http://www.gnu.org/licenses/>.
#

import argparse
import base64
import json
import logging
import mimetypes
import os
import re
import shutil
import xml.etree.ElementTree as ET
import zipfile
from functools import cmp_to_key
from typing import Dict, List
from urllib.parse import urlparse

import bs4.element
import requests
from bs4 import BeautifulSoup, Doctype
from termcolor import colored
from tqdm import tqdm

from .shared import (
    generate_names,
    build_opf_package,
    extract_isbn,
    extract_authors_from_openbook,
)
from ..libby import (
    USER_AGENT,
    LibbyClient,
    LibbyFormats,
    LibbyMediaTypes,
)
from ..overdrive import OverDriveClient
from ..utils import slugify

#
# Main processing logic for libby direct ebook and magazine loans
#

NAV_XHTMLTEMPLATE = """
<!DOCTYPE html>
<html xmlns="http://www.w3.org/1999/xhtml" xmlns:epub="http://www.idpf.org/2007/ops">
<head><title></title></head>
<body>
<nav epub:type="toc">
<h1>Contents</h1>
<ol id="toc"></ol>
</nav>
</body>
</html>
"""


def _build_ncx(media_info: Dict, openbook: Dict) -> ET.Element:
    """
    Build the ncx from openbook

    :param media_info:
    :param openbook:
    :return:
    """

    # References:
    # Version 2: https://idpf.org/epub/20/spec/OPF_2.0_final_spec.html#Section2.0
    # Version 3: https://www.w3.org/TR/epub-33/#sec-package-doc

    publication_identifier = (
        extract_isbn(
            media_info["formats"],
            [LibbyFormats.EBookOverdrive, LibbyFormats.MagazineOverDrive],
        )
        or media_info["id"]
    )

    ET.register_namespace("opf", "http://www.idpf.org/2007/opf")
    ET.register_namespace("dc", "http://purl.org/dc/elements/1.1/")
    ncx = ET.Element(
        "ncx",
        attrib={
            "version": "2005-1",
            "xmlns": "http://www.daisy.org/z3986/2005/ncx/",
            "xml:lang": "en",
        },
    )

    head = ET.SubElement(ncx, "head")
    ET.SubElement(
        head, "meta", attrib={"content": publication_identifier, "name": "dtb:uid"}
    )
    doc_title = ET.SubElement(ncx, "docTitle")
    doc_title_text = ET.SubElement(doc_title, "text")
    doc_title_text.text = openbook["title"]["main"]

    doc_author = ET.SubElement(ncx, "docAuthor")
    doc_author_text = ET.SubElement(doc_author, "text")
    doc_author_text.text = openbook["creator"][0]["name"]

    nav_map = ET.SubElement(ncx, "navMap")
    for i, item in enumerate(openbook["nav"]["toc"], start=1):
        nav_point = ET.SubElement(nav_map, "navPoint", attrib={"id": f"navPoint{i}"})
        nav_label = ET.SubElement(nav_point, "navLabel")
        nav_label_text = ET.SubElement(nav_label, "text")
        nav_label_text.text = item["title"]
        ET.SubElement(nav_point, "content", attrib={"src": item["path"]})
    return ncx


def _sanitise_opf_id(string_id: str) -> str:
    """
    OPF IDs cannot start with a number
    :param string_id:
    :return:
    """
    string_id = slugify(string_id)
    if string_id[0].isdigit():
        return f"id_{string_id}"
    return string_id


def _cleanup_soup(soup: BeautifulSoup, version: str = "2.0") -> None:
    """
    Tries to fix up book content pages to be epub-version compliant.

    :param soup:
    :param version:
    :return:
    """
    if version == "2.0":
        # v2 is a lot pickier about the acceptable elements and attributes
        modified_doctype = 'html PUBLIC "-//W3C//DTD XHTML 1.1//EN" "http://www.w3.org/TR/xhtml11/DTD/xhtml11.dtd"'
        for item in soup.contents:
            if isinstance(item, Doctype):
                item.replace_with(Doctype(modified_doctype))
                break
        remove_attributes = [
            # this list will not be complete, but we try
            "aria-label",
            "data-loc",
            "data-epub-type",
            "data-document-status",
            "data-xml-lang",
            "lang",
            "role",
            "epub:type",
            "epub:prefix",
        ]
        for attribute in remove_attributes:
            for tag in soup.find_all(attrs={attribute: True}):
                del tag[attribute]
        convert_tags = ["nav", "section"]  # this list will not be complete, but we try
        for tag in convert_tags:
            for invalid_tag in soup.find_all(tag):
                invalid_tag.name = "div"

    # known issues, this will not be complete
    for svg in soup.find_all("svg"):
        if not svg.get("xmlns"):
            svg["xmlns"] = "http://www.w3.org/2000/svg"
        if not svg.get("xmlns:xlink"):
            svg["xmlns:xlink"] = "http://www.w3.org/1999/xlink"
    convert_tags = ["figcaption"]
    for tag in convert_tags:
        for invalid_tag in soup.find_all(tag):
            invalid_tag.name = "div"
    remove_tags = ["base"]
    for tag in remove_tags:
        for remove_tag in soup.find_all(tag):
            remove_tag.decompose()

    html_tag = soup.find("html")
    if html_tag and isinstance(html_tag, bs4.element.Tag) and not html_tag.get("xmlns"):
        html_tag["xmlns"] = "http://www.w3.org/1999/xhtml"


def _sort_spine_entries(a: Dict, b: Dict, toc_pages: List[str]):
    """
    Sort spine according to TOC. For magazines, this is sometimes a
    problem where the sequence laid out in the spine does not align
    with the TOC, e.g. Mother Jones. If unsorted, the page through
    sequence does not match the actual TOC.

    :param a:
    :param b:
    :param toc_pages:
    :return:
    """
    try:
        a_index = toc_pages.index(a["-odread-original-path"])
    except ValueError:
        a_index = 999
    try:
        b_index = toc_pages.index(b["-odread-original-path"])
    except ValueError:
        b_index = 999

    if a_index != b_index:
        # sort order found via toc
        return -1 if a_index < b_index else 1

    return -1 if a["-odread-spine-position"] < b["-odread-spine-position"] else 1


def _filter_content(entry: Dict, media_info: Dict, toc_pages: List[str]):
    """
    Filter title contents that are not needed.

    :param entry:
    :param media_info:
    :param toc_pages:
    :return:
    """
    parsed_entry_url = urlparse(entry["url"])
    media_type, _ = mimetypes.guess_type(parsed_entry_url.path[1:])

    if media_info["type"]["id"] == LibbyMediaTypes.Magazine and media_type:
        if media_type.startswith("image/") and (
            parsed_entry_url.path.startswith("/pages/")
            or parsed_entry_url.path.startswith("/thumbnails/")
        ):
            return False
        if (
            media_type == "application/xhtml+xml"
            and parsed_entry_url.path[1:] not in toc_pages
        ):
            return False

    if parsed_entry_url.path.startswith("/_d/"):  # ebooks
        return False

    return True


def process_ebook_loan(
    loan: Dict,
    cover_path: str,
    openbook: Dict,
    rosters: List[Dict],
    libby_client: LibbyClient,
    args: argparse.Namespace,
    logger: logging.Logger,
) -> None:
    """
    Generates and return an ebook loan directly from Libby.

    :param loan:
    :param cover_path:
    :param openbook:
    :param rosters:
    :param libby_client:
    :param args:
    :param logger:
    :return:
    """

    book_folder, book_file_name, _ = generate_names(
        title=loan["title"],
        series=loan.get("series") or "",
        authors=extract_authors_from_openbook(openbook),
        edition=loan.get("edition") or "",
        args=args,
        logger=logger,
    )
    book_basename, _ = os.path.splitext(book_file_name)
    epub_file_path = f"{book_basename}.epub"
    epub_version = "3.0"

    book_meta_name = "META-INF"
    book_content_name = "OEBPS"
    book_meta_folder = os.path.join(book_folder, book_meta_name)
    book_content_folder = os.path.join(book_folder, book_content_name)
    for d in (book_meta_folder, book_content_folder):
        if not os.path.exists(d):
            os.makedirs(d)

    od_client = OverDriveClient(
        user_agent=USER_AGENT, timeout=args.timeout, retry=args.retries
    )
    media_info = od_client.media(loan["id"])

    if args.is_debug_mode:
        with open(os.path.join(book_folder, "media.json"), "w", encoding="utf-8") as f:
            json.dump(media_info, f, indent=2)

        with open(os.path.join(book_folder, "loan.json"), "w", encoding="utf-8") as f:
            json.dump(loan, f, indent=2)

        with open(
            os.path.join(book_folder, "rosters.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(rosters, f, indent=2)

        with open(
            os.path.join(book_folder, "openbook.json"), "w", encoding="utf-8"
        ) as f:
            json.dump(openbook, f, indent=2)

    title_contents: Dict = next(
        iter([r for r in rosters if r["group"] == "title-content"]), {}
    )
    headers = libby_client.default_headers()
    headers["Accept"] = "*/*"
    contents_re = re.compile(r"parent\.__bif_cfc0\(self,'(?P<base64_text>.+)'\)")

    openbook_toc = openbook["nav"]["toc"]
    toc_pages = [item["path"].split("#")[0] for item in openbook_toc]
    manifest_entries: List[Dict] = []

    title_content_entries = list(
        filter(
            lambda e: _filter_content(e, media_info, toc_pages),
            title_contents["entries"],
        )
    )
    progress_bar = tqdm(title_content_entries, disable=args.hide_progress)
    has_ncx = False
    has_nav = False

    # Used to patch magazine css that causes paged mode in calibre viewer to not work.
    # This expression is used to strip `overflow-x: hidden` from the css definition
    # for `#article-body`.
    patch_magazine_css_re = re.compile(
        r"(#article-body\s*\{[^{}]+?)overflow-x:\s*hidden;([^{}]+?})"
    )

    for entry in progress_bar:
        entry_url = entry["url"]
        parsed_entry_url = urlparse(entry_url)
        media_type, _ = mimetypes.guess_type(parsed_entry_url.path[1:])
        asset_folder = os.path.join(
            book_content_folder, os.path.dirname(parsed_entry_url.path[1:])
        )
        if media_type == "application/x-dtbncx+xml":
            has_ncx = True
        manifest_entry = {
            "href": parsed_entry_url.path[1:],
            "id": "ncx"
            if media_type == "application/x-dtbncx+xml"
            else _sanitise_opf_id(parsed_entry_url.path[1:]),
            "media-type": media_type,
        }
        if not os.path.exists(asset_folder):
            os.makedirs(asset_folder)
        asset_file_path = os.path.join(
            asset_folder, os.path.basename(parsed_entry_url.path)
        )
        if os.path.exists(asset_file_path):
            progress_bar.set_description(
                f"Already saved {os.path.basename(parsed_entry_url.path)}"
            )
            # check for properties
            if media_type == "application/xhtml+xml":
                with open(asset_file_path, "r", encoding="utf-8") as f_asset:
                    soup = BeautifulSoup(f_asset, features="html.parser")
                    if soup.find_all("svg"):
                        manifest_entry["properties"] = "svg"
                    # identify nav page
                    if soup.find(attrs={"epub:type": "toc"}):
                        manifest_entry["properties"] = "nav"
                        has_nav = True
        else:
            progress_bar.set_description(
                f"Downloading {os.path.basename(parsed_entry_url.path)}"
            )
            # use the libby client session because the required
            # auth cookies are set there
            res: requests.Response = libby_client.make_request(
                entry_url, headers=headers, return_res=True
            )

            # patch magazine css to fix rendering in calibre viewer
            if (
                media_info["type"]["id"] == LibbyMediaTypes.Magazine
                and media_type == "text/css"
            ):
                css_content = patch_magazine_css_re.sub(r"\1\2", res.text)
                with open(asset_file_path, "w", encoding="utf-8") as f_out:
                    f_out.write(css_content)
            elif media_type == "application/xhtml+xml":
                soup = BeautifulSoup(res.text, features="html.parser")
                script_ele = soup.find("script", attrs={"type": "text/javascript"})
                if script_ele and hasattr(script_ele, "string"):
                    mobj = contents_re.search(script_ele.string or "")
                    if mobj:
                        new_soup = BeautifulSoup(
                            base64.b64decode(mobj.group("base64_text")),
                            features="html.parser",
                        )
                        soup.body.replace_with(new_soup.body)  # type: ignore[arg-type,union-attr]
                _cleanup_soup(soup, version=epub_version)
                with open(asset_file_path, "w", encoding="utf-8") as f_out:
                    f_out.write(str(soup))
                if soup.find_all("svg"):
                    manifest_entry["properties"] = "svg"
                # identify nav page
                if soup.find(attrs={"epub:type": "toc"}):
                    manifest_entry["properties"] = "nav"
                    has_nav = True
            else:
                with open(asset_file_path, "wb") as f_out:
                    f_out.write(res.content)
        manifest_entries.append(manifest_entry)

    if not has_nav:
        # Generate nav - needed for magazines
        nav_soup = BeautifulSoup(NAV_XHTMLTEMPLATE, features="html.parser")
        nav_soup.find("title").append(loan["title"])  # type: ignore[union-attr]
        toc_ele = nav_soup.find(id="toc")
        for item in openbook_toc:
            li_ele = nav_soup.new_tag("li")
            a_ele = nav_soup.new_tag("a", attrs={"href": item["path"]})
            a_ele.append(item["title"])
            li_ele.append(a_ele)
            toc_ele.append(li_ele)  # type: ignore[union-attr]
        with open(
            os.path.join(book_content_folder, "nav.xhtml"), "w", encoding="utf-8"
        ) as f_nav:
            f_nav.write(str(nav_soup).strip())
        manifest_entries.append(
            {
                "href": "nav.xhtml",
                "id": "nav",
                "media-type": "application/xhtml+xml",
                "properties": "nav",
            }
        )

    if not has_ncx:
        # generate ncx for backward compat
        ncx = _build_ncx(media_info, openbook)
        tree = ET.ElementTree(ncx)
        tree.write(
            os.path.join(book_content_folder, "toc.ncx"),
            xml_declaration=True,
            encoding="utf-8",
        )
        manifest_entries.append(
            {
                "href": "toc.ncx",
                "id": "ncx",
                "media-type": "application/x-dtbncx+xml",
            }
        )
        has_ncx = True

    # create epub OPF
    opt_file_name = "package.opf"
    opf_file_path = os.path.join(book_content_folder, opt_file_name)
    package = build_opf_package(
        media_info,
        version=epub_version,
        loan_format=LibbyFormats.MagazineOverDrive
        if loan["type"]["id"] == LibbyMediaTypes.Magazine
        else LibbyFormats.EBookOverdrive,
    )
    # add manifest
    manifest = ET.SubElement(package, "manifest")
    for entry in manifest_entries:
        ET.SubElement(manifest, "item", attrib=entry)
    if cover_path:
        # add cover image separately since we can't identify which item is the cover
        shutil.copyfile(cover_path, os.path.join(book_content_folder, "cover.jpg"))
        ET.SubElement(
            manifest,
            "item",
            attrib={
                "id": "coverimage",
                "href": "cover.jpg",
                "media-type": "image/jpeg",
                "properties": "cover-image",
            },
        )
        metadata = package.find("metadata")
        if metadata:
            _ = ET.SubElement(
                metadata, "meta", attrib={"name": "cover", "content": "coverimage"}
            )

    # add spine
    spine = ET.SubElement(package, "spine")
    if has_ncx:
        spine.set("toc", "ncx")
    spine_entries = list(
        filter(
            lambda s: not (
                media_info["type"]["id"] == LibbyMediaTypes.Magazine
                and s["-odread-original-path"] not in toc_pages
            ),
            openbook["spine"],
        )
    )

    # Ignoring mypy error below because of https://github.com/python/mypy/issues/9372
    spine_entries = sorted(
        spine_entries, key=cmp_to_key(lambda a, b: _sort_spine_entries(a, b, toc_pages))  # type: ignore[misc]
    )
    for entry in spine_entries:
        if (
            media_info["type"]["id"] == LibbyMediaTypes.Magazine
            and entry["-odread-original-path"] not in toc_pages
        ):
            continue
        item_ref = ET.SubElement(spine, "itemref")
        item_ref.set("idref", _sanitise_opf_id(entry["-odread-original-path"]))

    # add guide
    if openbook.get("nav", {}).get("landmarks"):
        guide = ET.SubElement(package, "guide")
        for landmark in openbook["nav"]["landmarks"]:
            _ = ET.SubElement(
                guide,
                "reference",
                attrib={
                    "href": landmark["path"],
                    "title": landmark["title"],
                    "type": landmark["type"],
                },
            )

    if args.is_debug_mode:
        from xml.dom import minidom

        with open(opf_file_path, "w", encoding="utf-8") as f:
            f.write(
                minidom.parseString(ET.tostring(package, "utf-8")).toprettyxml(
                    indent="\t"
                )
            )
    else:
        tree = ET.ElementTree(package)
        tree.write(opf_file_path, xml_declaration=True, encoding="utf-8")
    logger.debug('Saved "%s"', opf_file_path)

    # create container.xml
    container_file_path = os.path.join(book_meta_folder, "container.xml")
    container = ET.Element(
        "container",
        attrib={
            "version": "1.0",
            "xmlns": "urn:oasis:names:tc:opendocument:xmlns:container",
        },
    )
    root_files = ET.SubElement(container, "rootfiles")
    _ = ET.SubElement(
        root_files,
        "rootfile",
        attrib={
            "full-path": os.path.join(book_content_name, opt_file_name),
            "media-type": "application/oebps-package+xml",
        },
    )
    tree = ET.ElementTree(container)
    tree.write(container_file_path, xml_declaration=True, encoding="utf-8")
    logger.debug('Saved "%s"', container_file_path)

    mimetype_file_path = os.path.join(book_folder, "mimetype")
    with open(mimetype_file_path, "w", encoding="utf-8") as f:
        f.write("application/epub+zip")

    # create epub zip
    with zipfile.ZipFile(
        epub_file_path, mode="w", compression=zipfile.ZIP_STORED
    ) as epub_zip:
        epub_zip.write(mimetype_file_path, arcname="mimetype")
        for folder_name, root_start in (
            (book_meta_name, book_meta_folder),
            (book_content_name, book_content_folder),
        ):
            epub_zip.write(book_meta_folder, arcname=folder_name)
            for path, _, files in os.walk(root_start):
                for file in files:
                    epub_zip.write(
                        str(os.path.join(path, file)),
                        arcname=os.path.relpath(
                            os.path.join(path, file), start=book_folder
                        ),
                    )
    logger.info('Saved "%s"', colored(epub_file_path, "magenta", attrs=["bold"]))

    # clean up
    if not args.is_debug_mode:
        for file_name in (
            "mimetype",
            "media.json",
            "openbook.json",
            "loan.json",
            "rosters.json",
        ):
            target = os.path.join(book_folder, file_name)
            if os.path.exists(target):
                os.remove(target)
        for folder in (book_content_folder, book_meta_folder):
            shutil.rmtree(folder, ignore_errors=True)
