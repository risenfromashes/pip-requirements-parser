
# Copyright (c) The pip developers (see AUTHORS.txt file)
# portions Copyright (C) 2016 Jason R Coombs <jaraco@jaraco.com>
# portions Copyright (C) nexB Inc. and others
# 
# SPDX-License-Identifier: MIT
#
# Permission is hereby granted, free of charge, to any person obtaining
# a copy of this software and associated documentation files (the
# "Software"), to deal in the Software without restriction, including
# without limitation the rights to use, copy, modify, merge, publish,
# distribute, sublicense, and/or sell copies of the Software, and to
# permit persons to whom the Software is furnished to do so, subject to
# the following conditions:
# 
# The above copyright notice and this permission notice shall be
# included in all copies or substantial portions of the Software.
# 
# THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND,
# EXPRESS OR IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF
# MERCHANTABILITY, FITNESS FOR A PARTICULAR PURPOSE AND
# NONINFRINGEMENT. IN NO EVENT SHALL THE AUTHORS OR COPYRIGHT HOLDERS BE
# LIABLE FOR ANY CLAIM, DAMAGES OR OTHER LIABILITY, WHETHER IN AN ACTION
# OF CONTRACT, TORT OR OTHERWISE, ARISING FROM, OUT OF OR IN CONNECTION
# WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE SOFTWARE.

import codecs
import locale
import functools
import hashlib
import io
import logging
import operator
import optparse
import os
import posixpath
import re
import shlex
import string
import sys
import urllib.parse
import urllib.request

from functools import partial
from optparse import Values
from optparse import Option
from optparse import OptionParser

from typing import (
    Any,
    BinaryIO,
    Callable,
    Collection,
    Dict,
    FrozenSet, 
    Iterable,
    Iterator,
    List,
    NamedTuple,
    NewType,
    Optional,
    Set,
    Tuple,
    Type,
    Union,
    cast,
)

from packaging.markers import Marker
from packaging.requirements import InvalidRequirement
from packaging.requirements import Requirement
from packaging.specifiers import Specifier
from packaging.specifiers import SpecifierSet
from packaging.tags import Tag
from packaging.utils import canonicalize_name

"""
A pip requirements files parser, doing it as well as pip does it.
Based on pip code itself.

The code is merged from multiple pip modules. And each pip code section is
tagged with comments:
    # PIPREQPARSE: from ... 
    # PIPREQPARSE: end from ...

We also kept the pip git line-level, blame history of all these modules.
"""

################################################################################
# This is the API for this module


class RequirementsFile:
    """
    This represents a requirements file. It contains the requirements and other
    pip-related options found in a requirerents file.
    """
    def __init__(self, filename: str, include_nested=False) -> None:
        """
        Initialise a new RequirementsFile from a ``filename`` path string.
        If ``include_nested`` is True also resolve, parse and load -r/-c
        requirements and constraints files referenced in the requirements file.
        """
        self.filename = filename

        self.format_control = FormatControl()
        self.options = Values()
        self.options.format_control = self.format_control

        finder = PackageFinder(format_control=self.format_control)

        self.install_requirements: List[InstallRequirement] = []
        self.invalid_lines: List[InvalidRequirementLine] = []
        self.comment_lines: List[CommentRequirementLine] = []

        for parsed in parse_requirements(
            filename=filename,
            finder=finder,
            options=self.options,
            include_nested=include_nested,
        ):
            if isinstance(parsed, InvalidRequirementLine):
                self.invalid_lines.append(parsed)
            elif isinstance(parsed, CommentRequirementLine):
                self.comment_lines.append(parsed)
            else:
                assert isinstance(parsed, ParsedRequirement)
                try:
                    req = install_req_from_parsed_requirement(parsed)

                    # we can also process some errors further down
                    if isinstance(parsed, InvalidRequirementLine):
                        self.invalid_lines.append(req)
                    else:
                        self.install_requirements.append(req)

                except Exception as e:

                    self.invalid_lines.append(InvalidRequirementLine(
                        requirement_line=parsed.requirement_line,
                        error_message=str(e),
                    ))

        self.find_links = finder.find_links
        self.index_urls =  finder.index_urls

    def to_dict(self, include_filename=False):
        """
        Return a mapping of plain Python objects for this RequirementsFile
        """
        return dict(
            find_links = self.find_links,
            index_urls = self.index_urls,
            options =  option_values_to_dict(self.options),
            install_requirements = [
                ir.to_dict() for ir in self.install_requirements
            ],
            invalid_lines = [
                upl.to_dict(include_filename=include_filename)
                for upl in self.invalid_lines
            ],
            comment_lines = [
                cl.to_dict(include_filename=include_filename)
                for cl in self.comment_lines
            ]
        )

    def dumps(self, unparse=False):
        """
        Return a requirements string representing this requirements file. If
        ``unparse`` is True, the requirements are reconstructed from the
        parsed data. Otherwise, the requirements string are assembled from the
        original normalized requirement text lines.
        """
        if unparse:
            pass
        else:
            requirement_lines = []

            for r in (
                self.install_requirements
                + self.invalid_lines
            ):
                requirement_lines.append(r.requirement_line)

            requirement_lines.extend(self.comment_lines)

            dumped = []

            previous_line_number = 0

            # by line number, then requirements before comments (so that eol comment come after)
            sort_by = lambda l: (l.line_number, isinstance(l, CommentRequirementLine))

            for rl in sorted(requirement_lines, key=sort_by):

                if previous_line_number == rl.line_number and isinstance(rl, CommentRequirementLine):
                    # trailing comment, append to end of previous line
                    previous = dumped[-1].rstrip("\n")
                    previous = f"{previous} {rl.line}\n"
                    dumped[-1] = previous
                else:
                    dumped.append(f"{rl.line}\n")

                previous_line_number = rl.line_number

            return "".join(dumped)


def option_values_to_dict(option_values):
 
    #list of known options: pre, prefer_binary, editables
    return {
        k: v.to_dict() if hasattr(v, "to_dict") else v 
        for k, v in option_values.__dict__.items()
    }

# end of API
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/utils/compat.py

# windows detection, covers cpython and ironpython
WINDOWS = (sys.platform.startswith("win") or
           (sys.platform == 'cli' and os.name == 'nt'))

# PIPREQPARSE: end from src/pip/_internal/utils/compat.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/utils/encoding.py

BOMS: List[Tuple[bytes, str]] = [
    (codecs.BOM_UTF8, "utf-8"),
    (codecs.BOM_UTF16, "utf-16"),
    (codecs.BOM_UTF16_BE, "utf-16-be"),
    (codecs.BOM_UTF16_LE, "utf-16-le"),
    (codecs.BOM_UTF32, "utf-32"),
    (codecs.BOM_UTF32_BE, "utf-32-be"),
    (codecs.BOM_UTF32_LE, "utf-32-le"),
]

ENCODING_RE = re.compile(br"coding[:=]\s*([-\w.]+)")


def auto_decode(data: bytes) -> str:
    """Check a bytes string for a BOM to correctly detect the encoding

    Fallback to locale.getpreferredencoding(False) like open() on Python3"""
    for bom, encoding in BOMS:
        if data.startswith(bom):
            return data[len(bom) :].decode(encoding)
    # Lets check the first two lines as in PEP263
    for line in data.split(b"\n")[:2]:
        if line[0:1] == b"#" and ENCODING_RE.search(line):
            result = ENCODING_RE.search(line)
            assert result is not None
            encoding = result.groups()[0].decode("ascii")
            return data.decode(encoding)
    return data.decode(
        locale.getpreferredencoding(False) or sys.getdefaultencoding(),
    )

# PIPREQPARSE: end from src/pip/_internal/utils/encoding.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/models/index.py

class PackageIndex:
    """Represents a Package Index and provides easier access to endpoints"""

    __slots__ = ["url", "netloc", "simple_url", "pypi_url", "file_storage_domain"]

    def __init__(self, url: str, file_storage_domain: str) -> None:
        super().__init__()
        self.url = url
        self.netloc = urllib.parse.urlsplit(url).netloc
        self.simple_url = self._url_for_path("simple")
        self.pypi_url = self._url_for_path("pypi")

        # This is part of a temporary hack used to block installs of PyPI
        # packages which depend on external urls only necessary until PyPI can
        # block such packages themselves
        self.file_storage_domain = file_storage_domain

    def _url_for_path(self, path: str) -> str:
        return urllib.parse.urljoin(self.url, path)


PyPI = PackageIndex("https://pypi.org/", file_storage_domain="files.pythonhosted.org")
TestPyPI = PackageIndex(
    "https://test.pypi.org/", file_storage_domain="test-files.pythonhosted.org"
)


# PIPREQPARSE: end from src/pip/_internal/models/index.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/exceptions.py

class PipError(Exception):
    """The base pip error."""


class InstallationError(PipError):
    """General exception during installation"""


class RequirementsFileParseError(InstallationError):
    """Raised when a general error occurs parsing a requirements file line."""


class CommandError(PipError):
    """Raised when there is an error in command-line arguments"""


class InvalidWheelFilename(InstallationError):
    """Invalid wheel filename."""


class HashError(InstallationError):
    """
    A failure to verify a package against known-good hashes
    """


class VcsHashUnsupported(HashError):
    """A hash was provided for a version-control-system-based requirement, but
    we don't have a method for hashing those."""


class DirectoryUrlHashUnsupported(HashError):
    """A hash was provided for a version-control-system-based requirement, but
    we don't have a method for hashing those."""


class HashMissing(HashError):
    """A hash was needed for a requirement but is absent."""


class HashUnpinned(HashError):
    """A requirement had a hash specified but was not pinned to a specific
    version."""


class HashMismatch(HashError):
    """
    Distribution file hash values don't match.
    """

# PIPREQPARSE: end from src/pip/_internal/exceptions.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/models/format_control.py


class FormatControl:
    """Helper for managing formats from which a package can be installed."""

    __slots__ = ["no_binary", "only_binary"]

    def __init__(
        self,
        no_binary: Optional[Set[str]] = None,
        only_binary: Optional[Set[str]] = None,
    ) -> None:
        if no_binary is None:
            no_binary = set()
        if only_binary is None:
            only_binary = set()

        self.no_binary = no_binary
        self.only_binary = only_binary

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, self.__class__):
            return NotImplemented

        if self.__slots__ != other.__slots__:
            return False

        return all(getattr(self, k) == getattr(other, k) for k in self.__slots__)

    def __repr__(self) -> str:
        return "{}({}, {})".format(
            self.__class__.__name__, self.no_binary, self.only_binary
        )

    @staticmethod
    def handle_mutual_excludes(value: str, target: Set[str], other: Set[str]) -> None:
        if value.startswith("-"):
            raise CommandError(
                "--no-binary / --only-binary option requires 1 argument."
            )
        new = value.split(",")
        while ":all:" in new:
            other.clear()
            target.clear()
            target.add(":all:")
            del new[: new.index(":all:") + 1]
            # Without a none, we want to discard everything as :all: covers it
            if ":none:" not in new:
                return
        for name in new:
            if name == ":none:":
                target.clear()
                continue
            name = canonicalize_name(name)
            other.discard(name)
            target.add(name)

    def get_allowed_formats(self, canonical_name: str) -> FrozenSet[str]:
        result = {"binary", "source"}
        if canonical_name in self.only_binary:
            result.discard("source")
        elif canonical_name in self.no_binary:
            result.discard("binary")
        elif ":all:" in self.only_binary:
            result.discard("source")
        elif ":all:" in self.no_binary:
            result.discard("binary")
        return frozenset(result)

    def disallow_binaries(self) -> None:
        self.handle_mutual_excludes(
            ":all:",
            self.no_binary,
            self.only_binary,
        )

    def to_dict(self):
        return dict(
            no_binary = sorted(self.no_binary),
            only_binary = sorted(self.only_binary),
        )

# PIPREQPARSE: end from src/pip/_internal/models/format_control.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/cli/cmdoptions.py:
# most callable renamed with cmdoptions_ prefix

def check_install_build_global(
    options: Values, check_options: Optional[Values] = None
) -> None:
    """Disable wheels if per-setup.py call options are set.

    :param options: The OptionParser options to update.
    :param check_options: The options to check, if not supplied defaults to
        options.
    """
    if check_options is None:
        check_options = options

    def getname(n: str) -> Optional[Any]:
        return getattr(check_options, n, None)

    names = ["build_options", "global_options", "install_options"]
    if any(map(getname, names)):
        control = options.format_control
        control.disallow_binaries()


index_url: Callable[..., Option] = partial(
    Option,
    "-i",
    "--index-url",
    "--pypi-url",
    dest="index_url",
    metavar="URL",
    default=PyPI.simple_url,
    help="Base URL of the Python Package Index (default %default). "
    "This should point to a repository compliant with PEP 503 "
    "(the simple repository API) or a local directory laid out "
    "in the same format.",
)


def extra_index_url() -> Option:
    return Option(
        "--extra-index-url",
        dest="extra_index_urls",
        metavar="URL",
        action="append",
        default=[],
        help="Extra URLs of package indexes to use in addition to "
        "--index-url. Should follow the same rules as "
        "--index-url.",
    )


no_index: Callable[..., Option] = partial(
    Option,
    "--no-index",
    dest="no_index",
    action="store_true",
    default=False,
    help="Ignore package index (only looking at --find-links URLs instead).",
)


def find_links() -> Option:
    return Option(
        "-f",
        "--find-links",
        dest="find_links",
        action="append",
        default=[],
        metavar="url",
        help="If a URL or path to an html file, then parse for links to "
        "archives such as sdist (.tar.gz) or wheel (.whl) files. "
        "If a local path or file:// URL that's a directory, "
        "then look for archives in the directory listing. "
        "Links to VCS project URLs are not supported.",
    )


def trusted_host() -> Option:
    return Option(
        "--trusted-host",
        dest="trusted_hosts",
        action="append",
        metavar="HOSTNAME",
        default=[],
        help="Mark this host or host:port pair as trusted, even though it "
        "does not have valid or any HTTPS.",
    )


def constraints() -> Option:
    return Option(
        "-c",
        "--constraint",
        dest="constraints",
        action="append",
        default=[],
        metavar="file",
        help="Constrain versions using the given constraints file. "
        "This option can be used multiple times.",
    )


def requirements() -> Option:
    return Option(
        "-r",
        "--requirement",
        dest="requirements",
        action="append",
        default=[],
        metavar="file",
        help="Install from the given requirements file. "
        "This option can be used multiple times.",
    )


def editable() -> Option:
    return Option(
        "-e",
        "--editable",
        dest="editables",
        action="append",
        default=[],
        metavar="path/url",
        help=(
            "Install a project in editable mode (i.e. setuptools "
            '"develop mode") from a local project path or a VCS url.'
        ),
    )


def _get_format_control(values: Values, option: Option) -> Any:
    """Get a format_control object."""
    return getattr(values, option.dest)


def _handle_no_binary(
    option: Option, opt_str: str, value: str, parser: OptionParser
) -> None:
    existing = _get_format_control(parser.values, option)
    FormatControl.handle_mutual_excludes(
        value,
        existing.no_binary,
        existing.only_binary,
    )


def _handle_only_binary(
    option: Option, opt_str: str, value: str, parser: OptionParser
) -> None:
    existing = _get_format_control(parser.values, option)
    FormatControl.handle_mutual_excludes(
        value,
        existing.only_binary,
        existing.no_binary,
    )


def no_binary() -> Option:
    format_control = FormatControl()
    return Option(
        "--no-binary",
        dest="format_control",
        action="callback",
        callback=_handle_no_binary,
        type="str",
        default=format_control,
        help="Do not use binary packages. Can be supplied multiple times, and "
        'each time adds to the existing value. Accepts either ":all:" to '
        'disable all binary packages, ":none:" to empty the set (notice '
        "the colons), or one or more package names with commas between "
        "them (no colons). Note that some packages are tricky to compile "
        "and may fail to install when this option is used on them.",
    )


def only_binary() -> Option:
    format_control = FormatControl()
    return Option(
        "--only-binary",
        dest="format_control",
        action="callback",
        callback=_handle_only_binary,
        type="str",
        default=format_control,
        help="Do not use source packages. Can be supplied multiple times, and "
        'each time adds to the existing value. Accepts either ":all:" to '
        'disable all source packages, ":none:" to empty the set, or one '
        "or more package names with commas between them. Packages "
        "without binary distributions will fail to install when this "
        "option is used on them.",
    )


def prefer_binary() -> Option:
    return Option(
        "--prefer-binary",
        dest="prefer_binary",
        action="store_true",
        default=False,
        help="Prefer older binary packages over newer source packages.",
    )


install_options: Callable[..., Option] = partial(
    Option,
    "--install-option",
    dest="install_options",
    action="append",
    metavar="options",
    help="Extra arguments to be supplied to the setup.py install "
    'command (use like --install-option="--install-scripts=/usr/local/'
    'bin"). Use multiple --install-option options to pass multiple '
    "options to setup.py install. If you are using an option with a "
    "directory path, be sure to use absolute path.",
)


global_options: Callable[..., Option] = partial(
    Option,
    "--global-option",
    dest="global_options",
    action="append",
    metavar="options",
    help="Extra global options to be supplied to the setup.py "
    "call before the install or bdist_wheel command.",
)


pre: Callable[..., Option] = partial(
    Option,
    "--pre",
    action="store_true",
    default=False,
    help="Include pre-release and development versions. By default, "
    "pip only finds stable versions.",
)



def _handle_merge_hash(
    option: Option, opt_str: str, value: str, parser: OptionParser
) -> None:
    """Given a value spelled "algo:digest", append the digest to a list
    pointed to in a dict by the algo name."""
    if not parser.values.hashes:
        parser.values.hashes = {}

    try:
        algo, digest = value.split(":", 1)
    except ValueError:
        algo = value
        digest = None
        # pass parser.error(
        #     "Arguments to {} must be a hash name "  # noqa
        #     "followed by a value, like --hash=sha256:"
        #     "abcde...".format(opt_str)
        # )
    # if algo not in STRONG_HASHES:
    #     parser.error(
    #         "Allowed hash algorithms for {} are {}.".format(  # noqa
    #             opt_str, ", ".join(STRONG_HASHES)
    #         )
    #     )
    parser.values.hashes.setdefault(algo, []).append(digest)


cmdoptions_hash: Callable[..., Option] = partial(
    Option,
    "--hash",
    # Hash values eventually end up in InstallRequirement.hashes due to
    # __dict__ copying in process_line().
    dest="hashes",
    action="callback",
    callback=_handle_merge_hash,
    type="string",
    help="Verify that the package's archive matches this "
    "hash before installing. Example: --hash=sha256:abcdef...",
)


require_hashes: Callable[..., Option] = partial(
    Option,
    "--require-hashes",
    dest="require_hashes",
    action="store_true",
    default=False,
    help="Require a hash to check each requirement against, for "
    "repeatable installs. This option is implied when any package in a "
    "requirements file has a --hash option.",
)

use_new_feature: Callable[..., Option] = partial(
    Option,
    "--use-feature",
    dest="features_enabled",
    metavar="feature",
    action="append",
    default=[],
    choices=["2020-resolver", "fast-deps", "in-tree-build"],
    help="Enable new functionality, that may be backward incompatible.",
)

# PIPREQPARSE: end from src/pip/_internal/cli/cmdoptions.py:
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/index/package_finder.py
# Significantly modified to work without network access

class PackageFinder:
    def __init__(
        self,
        find_links: Optional[List[str]] = None,
        index_urls: Optional[List[str]] = None,
        format_control: Optional[FormatControl] = None,
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """
        :param format_control: A FormatControl object, used to control
            the selection of source packages / binary packages when consulting
            the index and links.
        """
        self.format_control= format_control or FormatControl()
        self.find_links = find_links or []
        self.index_urls = index_urls or []

        self._allow_all_prereleases =False
        self._prefer_binary = True

    @property
    def allow_all_prereleases(self) -> bool:
        return self._allow_all_prereleases

    def set_allow_all_prereleases(self) -> None:
        self._allow_all_prereleases = True

    @property
    def prefer_binary(self) -> bool:
        return self._prefer_binary

    def set_prefer_binary(self) -> None:
        self._prefer_binary = True

# PIPREQPARSE: end from src/pip/_internal/index/package_finder.py
################################################################################

################################################################################
# PIPREQPARSE: from src/pip/_internal/req/req_file.py


class TextLine(NamedTuple):
    line_number: int
    line: str


class CommentLine(NamedTuple):
    line_number: int
    line: str

ReqFileLines = Iterable[Union[Tuple[int, str], TextLine,CommentLine]]

LineParser = Callable[[str], Tuple[str, Values]]

SCHEME_RE = re.compile(r"^(http|https|file):", re.I)
COMMENT_RE = re.compile(r"(^|\s+)(#.*)$")

SUPPORTED_OPTIONS: List[Callable[..., optparse.Option]] = [
    index_url,
    extra_index_url,
    no_index,
    constraints,
    requirements,
    editable,
    find_links,
    no_binary,
    only_binary,
    prefer_binary,
    require_hashes,
    pre,
    trusted_host,
    use_new_feature,
]

# options to be passed to requirements
SUPPORTED_OPTIONS_REQ: List[Callable[..., optparse.Option]] = [
    install_options,
    global_options,
    cmdoptions_hash,
]

# the 'dest' string values
SUPPORTED_OPTIONS_REQ_DEST = [str(o().dest) for o in SUPPORTED_OPTIONS_REQ]


class ParsedRequirement:
    def __init__(
        self,
        requirement_string: str,
        is_editable: bool,
        is_constraint: bool,
        options: Optional[Dict[str, Any]] = None,
        requirement_line: Optional["RequirementLine"] = None,
    ) -> None:
        self.requirement_string = requirement_string
        self.is_editable = is_editable
        self.is_constraint = is_constraint
        self.options = options
        self.requirement_line = requirement_line


class RequirementLine:
    """
    A line from a requirement ``filename``. This is a logical line with folded
    continuations where ``line_number`` is the first line number where this
    logical line started.
    """
    def __init__(
        self,
        line: str,
        line_number: Optional[int] = 0,
        filename: Optional[str] = None,
    ) -> None:
        self.line =line 
        self.filename = filename
        self.line_number = line_number

    def to_dict(self, include_filename=False):
        data = dict(
            line_number=self.line_number,
            line=self.line,
        )
        if include_filename:
            data.update(dict(filename=self.filename))

        return data

    def __repr__(self):
        return (
            f"{self.__class__.__name__}("
                f"line_number={self.line_number!r}, "
                f"line={self.line!r}, "
                f"filename={self.filename!r}"
            ")"
        )

    def __eq__(self, other):
        return (
            isinstance(other, self.__class__) and
            self.to_dict(include_filename=True)
                == other.to_dict(include_filename=True)
        )


class CommentRequirementLine(RequirementLine):
    """
    This represents the comment portion of a line in a requirements file. 
    """


class InvalidRequirementLine:
    """
    This represents an unparsable or invalid line of a requirements file.
    """
    def __init__(
        self,
        requirement_line: RequirementLine,
        error_message: str,
    ) -> None:
        self.requirement_line = requirement_line
        self.error_message = error_message

    def to_dict(self, include_filename=False):
        data = self.requirement_line.to_dict(include_filename=include_filename)
        data.update(error_message=self.error_message)
        return data

    def __repr__(self):
        return (
            f"{self.__class__.__name__}("
                f"requirement_line={self.requirement_line!r}, "
                f"error_message={self.error_message!r}, "
            ")"
        )
    def __eq__(self, other):
        return (
            isinstance(other, self.__class__) and
            self.to_dict(include_filename=True) 
                == other.to_dict(include_filename=True)
        )


class ParsedLine:
    def __init__(
        self,
        requirement_line: RequirementLine,
        requirement_string: str,
        options: Values,
        is_constraint: bool,
    ) -> None:

        self.requirement_line = requirement_line
        self.options = options
        self.is_constraint = is_constraint

        if requirement_string:
            self.is_requirement = True
            self.is_editable = False
            self.requirement_string = requirement_string
        elif options.editables:
            self.is_requirement = True
            self.is_editable = True
            # We don't support multiple -e on one line
            # FIXME: report warning if there are more than one
            self.requirement_string = options.editables[0]
        else:
            self.is_requirement = False


def parse_requirements(
    filename: str,
    finder: Optional[PackageFinder] = None,
    options: Optional[optparse.Values] = None,
    is_constraint: bool = False,
    include_nested: bool = True,
) -> Iterator[Union[ParsedRequirement, InvalidRequirementLine, CommentRequirementLine]]:
    """Parse a requirements file and yield ParsedRequirement,
    InvalidRequirementLine or CommentRequirementLine instances.

    :param filename:    Path or url of requirements file.
    :param finder:      Instance of PackageFinder used to collect options.
    :param options:     cli options.
    :param is_constraint:  If true, parsing a constraint file rather than
        requirements file.
    :param include_nested: if true, also load and parse -r/--requirements
        and -c/--constraints nested files.
    """
    line_parser = get_line_parser(finder)
    parser = RequirementsFileParser(line_parser)

    for parsed_line in parser.parse(
        filename=filename,
        is_constraint=is_constraint,
        include_nested=include_nested,
    ):

        if isinstance(parsed_line, ParsedLine):
            parsed_req = handle_line(
                parsed_line=parsed_line, options=options, finder=finder,
            )
            if parsed_req is not None:
                yield parsed_req

        else:
            assert isinstance(
                parsed_line, (InvalidRequirementLine, CommentRequirementLine,)
            )
            yield parsed_line


def preprocess(content: str) -> ReqFileLines:
    """Split, filter, and join lines, and return a line iterator.
    This contains both CommentLine and TextLine.

    :param content: the content of the requirements file
    """
    lines_enum: ReqFileLines = enumerate(content.splitlines(), start=1)
    lines_enum = join_lines(lines_enum)
    lines_and_comments_enum = split_comments(lines_enum)
    return lines_and_comments_enum


def handle_requirement_line(
    parsed_line: ParsedLine,
    options: Optional[optparse.Values] = None,
) -> ParsedRequirement:

    assert parsed_line.is_requirement

    if parsed_line.is_editable:
        # For editable requirements, we don't support per-requirement
        # options, so just return the parsed requirement.
        return ParsedRequirement(
            requirement_string=parsed_line.requirement_string,
            is_editable=parsed_line.is_editable,
            is_constraint=parsed_line.is_constraint,
            requirement_line=parsed_line.requirement_line,
        )
    else:
        if options:
            # Disable wheels if the user has specified build options
            check_install_build_global(options, parsed_line.options)

        # get the options that apply to requirements
        req_options = {}
        for dest in SUPPORTED_OPTIONS_REQ_DEST:
            if dest in parsed_line.options.__dict__ and parsed_line.options.__dict__[dest]:
                req_options[dest] = parsed_line.options.__dict__[dest]

        return ParsedRequirement(
            requirement_string=parsed_line.requirement_string,
            is_editable=parsed_line.is_editable,
            is_constraint=parsed_line.is_constraint,
            options=req_options,
            requirement_line=parsed_line.requirement_line,
        )


def handle_option_line(
    opts: Values,
    finder: Optional[PackageFinder] = None,
    options: Optional[optparse.Values] = None,
) -> None:

    if options:
        # percolate options upward
        if opts.require_hashes:
            options.require_hashes = opts.require_hashes
        if opts.features_enabled:
            options.features_enabled.extend(
                f for f in opts.features_enabled
                if f not in options.features_enabled
            )

    # set finder options
    if finder:
        if opts.index_url:
            finder.index_urls.append(opts.index_url)

        if opts.extra_index_urls:
            finder.index_urls.extend(opts.extra_index_urls)
        
        if opts.find_links:
            finder.find_links.extend(opts.find_links)

        if opts.pre:
            finder.set_allow_all_prereleases()

        if opts.prefer_binary:
            finder.set_prefer_binary()


def handle_line(
    parsed_line: ParsedLine,
    options: Optional[optparse.Values] = None,
    finder: Optional[PackageFinder] = None,
) -> Optional[ParsedRequirement]:
    """Handle a single parsed requirements line; This can result in
    creating/yielding requirements, or updating the finder.

    :param parsed_line:        The parsed line to be processed.
    :param options:     CLI options.
    :param finder:      The finder - updated by non-requirement lines.

    Returns a ParsedRequirement object if the line is a requirement line,
    otherwise returns None.

    For lines that contain requirements, the only options that have an effect
    are from SUPPORTED_OPTIONS_REQ, and they are scoped to the
    requirement. Other options from SUPPORTED_OPTIONS may be present, but are
    ignored.

    For lines that do not contain requirements, the only options that have an
    effect are from SUPPORTED_OPTIONS. Options from SUPPORTED_OPTIONS_REQ may
    be present, but are ignored. These lines may contain multiple options
    (although our docs imply only one is supported), and all our parsed and
    affect the finder.
    """

    if parsed_line.is_requirement:
        return handle_requirement_line(parsed_line=parsed_line, options=options)

    else:
        handle_option_line(
            opts=parsed_line.options,
            finder=finder,
            options=options,
        )
        return None


class RequirementsFileParser:

    def __init__(self, line_parser: LineParser) -> None:
        self._line_parser = line_parser

    def parse(
        self, 
        filename: str, 
        is_constraint: bool, 
        include_nested: bool = True
    ) -> Iterator[Union[ParsedLine, InvalidRequirementLine, CommentRequirementLine]]:
        """
        Parse a requirements ``filename``, yielding ParsedLine,
        InvalidRequirementLine or CommentRequirementLine.

        If ``include_nested`` is True, also load nested requirements and
        constraints files -r/--requirements and -c/--constraints recursively.

        If ``is_constraint`` is True, tag the ParsedLine as being "constraint"
        originating from a "constraint" file rather than a requirements file.
        """
        yield from self._parse_and_recurse(
            filename=filename,
            is_constraint=is_constraint,
            include_nested=include_nested,
        )

    def _parse_and_recurse(
        self, 
        filename: str, 
        is_constraint: bool, 
        include_nested: bool = True
    ) -> Iterator[Union[ParsedLine, InvalidRequirementLine, CommentRequirementLine]]:
        """
        Parse a requirements ``filename``, yielding ParsedLine,
        InvalidRequirementLine or CommentRequirementLine.

        If ``include_nested`` is True, also load nested requirements and
        constraints files -r/--requirements and -c/--constraints recursively.

        If ``is_constraint`` is True, tag the ParsedLine as being "constraint"
        originating from a "constraint" file rather than a requirements file.
        """
        for line in self._parse_file(filename=filename, is_constraint=is_constraint):

            if (include_nested 
                and isinstance(line, ParsedLine) 
                and not line.is_requirement and
                (line.options.requirements or line.options.constraints)
            ):
                # parse a nested requirements file
                if line.options.requirements:
                    if len(line.options.requirements) !=1:
                        # FIXME: this should be an error condition
                        pass
                    req_path = line.options.requirements[0]
                    is_nested_constraint = False

                else:
                    if len(line.options.constraints) !=1:
                        # FIXME: this should be an error condition
                        pass
                    req_path = line.options.constraints[0]
                    is_nested_constraint = True

                # original file is over http
                if SCHEME_RE.search(filename):
                    # do a url join so relative paths work
                    req_path = urllib.parse.urljoin(filename, req_path)
                
                # original file and nested file are paths
                elif not SCHEME_RE.search(req_path):
                    # do a join so relative paths work
                    req_path = os.path.join(
                        os.path.dirname(filename),
                        req_path,
                    )

                yield from self._parse_and_recurse(
                    filename=req_path, 
                    is_constraint=is_nested_constraint,
                    include_nested=include_nested,
                )
            else:
                yield line

    def _parse_file(self, filename: str, is_constraint: bool
    ) -> Iterator[Union[ParsedLine, InvalidRequirementLine, CommentRequirementLine]]:
        """
        Parse a single requirements ``filename``, yielding ParsedLine,
        InvalidRequirementLine or CommentRequirementLine.

        If ``is_constraint`` is True, tag the ParsedLine as being "constraint"
        originating from a "constraint" file rather than a requirements file.
        """
        content = get_file_content(filename)
        numbered_lines = preprocess(content)

        for numbered_line in numbered_lines:
            line_number, line = numbered_line

            if isinstance(numbered_line, CommentLine):
                yield CommentRequirementLine(
                    line=line,
                    line_number=line_number,
                    filename=filename,
                )
                continue

            requirement_line = RequirementLine(
                line=line,
                line_number=line_number,
                filename=filename,
            )
            try:
                requirement_string, options = self._line_parser(line)
                yield ParsedLine(
                    requirement_string=requirement_string,
                    options=options,
                    is_constraint=is_constraint,
                    requirement_line=requirement_line,
                )
            except Exception as e:
                # return offending line
                yield InvalidRequirementLine(
                    requirement_line=requirement_line,
                    error_message=str(e),
                )


def get_line_parser(finder: Optional["PackageFinder"]) -> LineParser:
    def parse_line(line: str) -> Tuple[str, Values]:
        # Build new parser for each line since it accumulates appendable
        # options.
        parser = build_parser()
        defaults = parser.get_default_values()
        defaults.index_url = None
        if finder:
            defaults.format_control = finder.format_control

        args_str, options_str = break_args_options(line)

        opts, _ = parser.parse_args(shlex.split(options_str), defaults)

        return args_str, opts

    return parse_line


def break_args_options(line: str) -> Tuple[str, str]:
    """Break up the line into an args and options string.  We only want to shlex
    (and then optparse) the options, not the args.  args can contain markers
    which are corrupted by shlex.
    """
    tokens = line.split(" ")
    args = []
    options = tokens[:]
    for token in tokens:
        if token.startswith("-") or token.startswith("--"):
            break
        else:
            args.append(token)
            options.pop(0)
    return " ".join(args), " ".join(options)


class OptionParsingError(Exception):
    def __init__(self, msg: str) -> None:
        self.msg = msg


def build_parser() -> optparse.OptionParser:
    """
    Return a parser for parsing requirement lines
    """
    parser = optparse.OptionParser(add_help_option=False)

    option_factories = SUPPORTED_OPTIONS + SUPPORTED_OPTIONS_REQ
    for option_factory in option_factories:
        option = option_factory()
        parser.add_option(option)

    # By default optparse sys.exits on parsing errors. We want to wrap
    # that in our own exception.
    def parser_exit(self: Any, msg: str) -> "NoReturn":
        raise OptionParsingError(msg)

    # NOTE: mypy disallows assigning to a method
    #       https://github.com/python/mypy/issues/2427
    parser.exit = parser_exit  # type: ignore

    return parser


def join_lines(lines_enum: ReqFileLines) -> ReqFileLines:
    """Joins a line ending in '\' with the previous line (except when following
    comments).  The joined line takes on the index of the first line.
    """
    primary_line_number = None
    new_line: List[str] = []
    for line_number, line in lines_enum:
        if not line.endswith("\\") or COMMENT_RE.match(line):
            if COMMENT_RE.match(line):
                # this ensures comments are always matched later
                line = " " + line
            if new_line:
                new_line.append(line)
                assert primary_line_number is not None
                yield primary_line_number, "".join(new_line)
                new_line = []
            else:
                yield line_number, line
        else:
            if not new_line:
                primary_line_number = line_number
            new_line.append(line.strip("\\"))

    # last line contains \
    if new_line:
        assert primary_line_number is not None
        yield primary_line_number, "".join(new_line)

    # TODO: handle space after '\'.


def split_comments(lines_enum: ReqFileLines) -> ReqFileLines:
    """
    Split comments from text, strip text and filter empty lines.
    Yield TextLine or Commentline
    """
    for line_number, line in lines_enum:
        parts = [l.strip() for l in COMMENT_RE.split(line) if l.strip()]

        if len(parts) == 1:
            part = parts[0]
            if part.startswith('#'):
                yield CommentLine(line_number=line_number, line=part)
            else:
                yield TextLine(line_number=line_number, line=part)

        elif len(parts) == 2:
            line, comment = parts
            yield TextLine(line_number=line_number, line=line)
            yield CommentLine(line_number=line_number, line=comment)

        else:
            if parts:
                # this should not ever happen
                raise Exception(f"Invalid line/comment: {line!r}")


def get_file_content(filename: str) -> str:
    """
    Return the unicode text content of a filename.
    Respects # -*- coding: declarations on the retrieved files.

    :param filename:         File path.
    """
    try:
        with open(filename, "rb") as f:
            content = auto_decode(f.read())
    except OSError as exc:
        raise InstallationError(
            f"Could not open requirements file: {filename}|n{exc}"
        )
    return content

# PIPREQPARSE: end src/pip/_internal/req/from req_file.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/utils/urls.py

def get_url_scheme(url: str) -> Optional[str]:
    if ":" not in url:
        return None
    return url.split(":", 1)[0].lower()


def url_to_path(url: str) -> str:
    """
    Convert a file: URL to a path.
    """
    assert url.startswith(
        "file:"
    ), f"You can only turn file: urls into filenames (not {url!r})"

    _, netloc, path, _, _ = urllib.parse.urlsplit(url)

    if not netloc or netloc == "localhost":
        # According to RFC 8089, same as empty authority.
        netloc = ""
    elif WINDOWS:
        # If we have a UNC path, prepend UNC share notation.
        netloc = "\\\\" + netloc
    else:
        raise ValueError(
            f"non-local file URIs are not supported on this platform: {url!r}"
        )

    path = urllib.request.url2pathname(netloc + path)

    # On Windows, urlsplit parses the path as something like "/C:/Users/foo".
    # This creates issues for path-related functions like io.open(), so we try
    # to detect and strip the leading slash.
    if (
        WINDOWS
        and not netloc  # Not UNC.
        and len(path) >= 3
        and path[0] == "/"  # Leading slash to strip.
        and path[1] in string.ascii_letters  # Drive letter.
        and path[2:4] in (":", ":/")  # Colon + end of string, or colon + absolute path.
    ):
        path = path[1:]

    return path

# PIPREQPARSE: end from src/pip/_internal/utils/urls.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/utils/hashes.py

# The recommended hash algo of the moment. Change this whenever the state of
# the art changes; it won't hurt backward compatibility.
FAVORITE_HASH = "sha256"


# Names of hashlib algorithms allowed by the --hash option and ``pip hash``
# Currently, those are the ones at least as collision-resistant as sha256.
STRONG_HASHES = ["sha256", "sha384", "sha512"]


class Hashes:
    """A wrapper that builds multiple hashes at once and checks them against
    known-good values

    """

    def __init__(self, hashes: Dict[str, List[str]] = None) -> None:
        """
        :param hashes: A dict of algorithm names pointing to lists of allowed
            hex digests
        """
        allowed = {}
        if hashes is not None:
            for alg, keys in hashes.items():
                # Make sure values are always sorted (to ease equality checks)
                allowed[alg] = sorted(keys)
        self._allowed = allowed

    def __and__(self, other: "Hashes") -> "Hashes":
        if not isinstance(other, Hashes):
            return NotImplemented

        # If either of the Hashes object is entirely empty (i.e. no hash
        # specified at all), all hashes from the other object are allowed.
        if not other:
            return self
        if not self:
            return other

        # Otherwise only hashes that present in both objects are allowed.
        new = {}
        for alg, values in other._allowed.items():
            if alg not in self._allowed:
                continue
            new[alg] = [v for v in values if v in self._allowed[alg]]
        return Hashes(new)

    @property
    def digest_count(self) -> int:
        return sum(len(digests) for digests in self._allowed.values())

    def is_hash_allowed(self, hash_name: str, hex_digest: str) -> bool:
        """Return whether the given hex digest is allowed."""
        return hex_digest in self._allowed.get(hash_name, [])

    def check_against_chunks(self, chunks: Iterator[bytes]) -> None:
        """Check good hashes against ones built from iterable of chunks of
        data.

        Raise HashMismatch if none match.

        """
        gots = {}
        for hash_name in self._allowed.keys():
            try:
                gots[hash_name] = hashlib.new(hash_name)
            except (ValueError, TypeError):
                raise InstallationError(f"Unknown hash name: {hash_name}")

        for chunk in chunks:
            for hsh in gots.values():
                hsh.update(chunk)

        for hash_name, got in gots.items():
            if got.hexdigest() in self._allowed[hash_name]:
                return
        self._raise(gots)

    def _raise(self, gots: Dict[str, "_Hash"]) -> "NoReturn":
        raise HashMismatch(self._allowed, gots)

    def check_against_file(self, file: BinaryIO) -> None:
        """Check good hashes against a file-like object

        Raise HashMismatch if none match.

        """
        return self.check_against_chunks(read_chunks(file))

    def check_against_path(self, path: str) -> None:
        with open(path, "rb") as file:
            return self.check_against_file(file)

    def __bool__(self) -> bool:
        """Return whether I know any known-good hashes."""
        return bool(self._allowed)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Hashes):
            return NotImplemented
        return self._allowed == other._allowed

    def __hash__(self) -> int:
        return hash(
            ",".join(
                sorted(
                    ":".join((alg, digest))
                    for alg, digest_list in self._allowed.items()
                    for digest in digest_list
                )
            )
        )


class MissingHashes(Hashes):
    """A workalike for Hashes used when we're missing a hash for a requirement

    It computes the actual hash of the requirement and raises a HashMissing
    exception showing it to the user.

    """

    def __init__(self) -> None:
        """Don't offer the ``hashes`` kwarg."""
        # Pass our favorite hash in to generate a "gotten hash". With the
        # empty list, it will never match, so an error will always raise.
        super().__init__(hashes={FAVORITE_HASH: []})

    def _raise(self, gots: Dict[str, "_Hash"]) -> "NoReturn":
        raise HashMissing(gots[FAVORITE_HASH].hexdigest())

# PIPREQPARSE: from src/pip/_internal/utils/hashes.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/utils/models.py

class KeyBasedCompareMixin:
    """Provides comparison capabilities that is based on a key"""

    __slots__ = ["_compare_key", "_defining_class"]

    def __init__(self, key: Any, defining_class: Type["KeyBasedCompareMixin"]) -> None:
        self._compare_key = key
        self._defining_class = defining_class

    def __hash__(self) -> int:
        return hash(self._compare_key)

    def __lt__(self, other: Any) -> bool:
        return self._compare(other, operator.__lt__)

    def __le__(self, other: Any) -> bool:
        return self._compare(other, operator.__le__)

    def __gt__(self, other: Any) -> bool:
        return self._compare(other, operator.__gt__)

    def __ge__(self, other: Any) -> bool:
        return self._compare(other, operator.__ge__)

    def __eq__(self, other: Any) -> bool:
        return self._compare(other, operator.__eq__)

    def _compare(self, other: Any, method: Callable[[Any, Any], bool]) -> bool:
        if not isinstance(other, self._defining_class):
            return NotImplemented

        return method(self._compare_key, other._compare_key)

# PIPREQPARSE: end from src/pip/_internal/utils/models.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/utils/packaging.py

NormalizedExtra = NewType("NormalizedExtra", str)


def safe_extra(extra: str) -> NormalizedExtra:
    """Convert an arbitrary string to a standard 'extra' name

    Any runs of non-alphanumeric characters are replaced with a single '_',
    and the result is always lowercased.

    This function is duplicated from ``pkg_resources``. Note that this is not
    the same to either ``canonicalize_name`` or ``_egg_link_name``.
    """
    return cast(NormalizedExtra, re.sub("[^A-Za-z0-9.-]+", "_", extra).lower())

# PIPREQPARSE: end from src/pip/_internal/utils/packaging.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/models/link.py

_SUPPORTED_HASHES = ("sha1", "sha224", "sha384", "sha256", "sha512", "md5")


class Link(KeyBasedCompareMixin):
    """Represents a parsed link from a Package Index's simple URL"""

    __slots__ = [
        "_parsed_url",
        "_url",
    ]

    def __init__(
        self,
        url: str,
    ) -> None:
        """
        :param url: url of the resource pointed to (href of the link)
        """

        self._parsed_url = urllib.parse.urlsplit(url)
        # Store the url as a private attribute to prevent accidentally
        # trying to set a new value.
        self._url = url
        super().__init__(key=url, defining_class=Link)

    def __str__(self) -> str:
        return self.url

    def __repr__(self) -> str:
        return f"<Link {self}>"

    @property
    def url(self) -> str:
        return self._url

    @property
    def filename(self) -> str:
        path = self.path.rstrip("/")
        name = posixpath.basename(path)
        if not name:
            # Make sure we don't leak auth information if the netloc
            # includes a username and password.
            netloc, _user_pass = split_auth_from_netloc(self.netloc)
            return netloc

        name = urllib.parse.unquote(name)
        assert name, f"URL {self._url!r} produced no filename"
        return name

    @property
    def file_path(self) -> str:
        return url_to_path(self.url)

    @property
    def scheme(self) -> str:
        return self._parsed_url.scheme

    @property
    def netloc(self) -> str:
        """
        This can contain auth information.
        """
        return self._parsed_url.netloc

    @property
    def path(self) -> str:
        return urllib.parse.unquote(self._parsed_url.path)

    def splitext(self) -> Tuple[str, str]:
        return splitext(posixpath.basename(self.path.rstrip("/")))

    @property
    def ext(self) -> str:
        return self.splitext()[1]

    @property
    def url_without_fragment(self) -> str:
        scheme, netloc, path, query, _fragment = self._parsed_url
        return urllib.parse.urlunsplit((scheme, netloc, path, query, ""))

    _egg_fragment_re = re.compile(r"[#&]egg=([^&]*)")

    @property
    def egg_fragment(self) -> Optional[str]:
        match = self._egg_fragment_re.search(self._url)
        if not match:
            return None
        return match.group(1)

    _subdirectory_fragment_re = re.compile(r"[#&]subdirectory=([^&]*)")

    @property
    def subdirectory_fragment(self) -> Optional[str]:
        match = self._subdirectory_fragment_re.search(self._url)
        if not match:
            return None
        return match.group(1)

    _hash_re = re.compile(
        r"({choices})=([a-f0-9]+)".format(choices="|".join(_SUPPORTED_HASHES))
    )

    @property
    def hash(self) -> Optional[str]:
        match = self._hash_re.search(self._url)
        if match:
            return match.group(2)
        return None

    @property
    def hash_name(self) -> Optional[str]:
        match = self._hash_re.search(self._url)
        if match:
            return match.group(1)
        return None

    @property
    def show_url(self) -> str:
        return posixpath.basename(self._url.split("#", 1)[0].split("?", 1)[0])

    @property
    def is_file(self) -> bool:
        return self.scheme == "file"

    @property
    def is_wheel(self) -> bool:
        return self.ext == WHEEL_EXTENSION

    @property
    def is_vcs(self) -> bool:
        return self.scheme in vcs_all_schemes

    @property
    def has_hash(self) -> bool:
        return self.hash_name is not None

    def is_hash_allowed(self, hashes: Optional[Hashes]) -> bool:
        """
        Return True if the link has a hash and it is allowed.
        """
        if hashes is None or not self.has_hash:
            return False
        # Assert non-None so mypy knows self.hash_name and self.hash are str.
        assert self.hash_name is not None
        assert self.hash is not None

        return hashes.is_hash_allowed(self.hash_name, hex_digest=self.hash)


class _CleanResult(NamedTuple):
    """Convert link for equivalency check.

    This is used in the resolver to check whether two URL-specified requirements
    likely point to the same distribution and can be considered equivalent. This
    equivalency logic avoids comparing URLs literally, which can be too strict
    (e.g. "a=1&b=2" vs "b=2&a=1") and produce conflicts unexpecting to users.

    Currently this does three things:

    1. Drop the basic auth part. This is technically wrong since a server can
       serve different content based on auth, but if it does that, it is even
       impossible to guarantee two URLs without auth are equivalent, since
       the user can input different auth information when prompted. So the
       practical solution is to assume the auth doesn't affect the response.
    2. Parse the query to avoid the ordering issue. Note that ordering under the
       same key in the query are NOT cleaned; i.e. "a=1&a=2" and "a=2&a=1" are
       still considered different.
    3. Explicitly drop most of the fragment part, except ``subdirectory=`` and
       hash values, since it should have no impact the downloaded content. Note
       that this drops the "egg=" part historically used to denote the requested
       project (and extras), which is wrong in the strictest sense, but too many
       people are supplying it inconsistently to cause superfluous resolution
       conflicts, so we choose to also ignore them.
    """

    parsed: urllib.parse.SplitResult
    query: Dict[str, List[str]]
    subdirectory: str
    hashes: Dict[str, str]


def _clean_link(link: Link) -> _CleanResult:
    parsed = link._parsed_url
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    # According to RFC 8089, an empty host in file: means localhost.
    if parsed.scheme == "file" and not netloc:
        netloc = "localhost"
    fragment = urllib.parse.parse_qs(parsed.fragment)
    if "egg" in fragment:
        logger.debug("Ignoring egg= fragment in %s", link)
    try:
        # If there are multiple subdirectory values, use the first one.
        # This matches the behavior of Link.subdirectory_fragment.
        subdirectory = fragment["subdirectory"][0]
    except (IndexError, KeyError):
        subdirectory = ""
    # If there are multiple hash values under the same algorithm, use the
    # first one. This matches the behavior of Link.hash_value.
    hashes = {k: fragment[k][0] for k in _SUPPORTED_HASHES if k in fragment}
    return _CleanResult(
        parsed=parsed._replace(netloc=netloc, query="", fragment=""),
        query=urllib.parse.parse_qs(parsed.query),
        subdirectory=subdirectory,
        hashes=hashes,
    )


@functools.lru_cache(maxsize=None)
def links_equivalent(link1: Link, link2: Link) -> bool:
    return _clean_link(link1) == _clean_link(link2)

# PIPREQPARSE: end from src/pip/_internal/models/link.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/req/req_install.py

class InstallRequirement:
    """
    Represents something that may be installed later on, may have information
    about where to fetch the relevant requirement and also contains logic for
    installing the said requirement.
    """

    def __init__(
        self,
        req: Optional[Requirement],
        requirement_line: Optional[Union[str, "RequirementLine"]],
        is_editable: bool = False,
        link: Optional[Link] = None,
        markers: Optional[Marker] = None,
        install_options: Optional[List[str]] = None,
        global_options: Optional[List[str]] = None,
        hash_options: Optional[Dict[str, List[str]]] = None,
        is_constraint: bool = False,
        extras: Collection[str] = (),
    ) -> None:
        assert req is None or isinstance(req, Requirement), req
        self.req = req
        self.requirement_line = requirement_line
        self.is_constraint = is_constraint
        self.is_editable = is_editable

        if link is None and req and req.url:
            # PEP 508 URL requirement
            link = Link(req.url)
        self.link = link

        if extras:
            self.extras = extras
        elif req:
            self.extras = {safe_extra(extra) for extra in req.extras}
        else:
            self.extras = set()

        if markers is None and req:
            markers = req.marker
        self.markers = markers

        # Supplied options
        self.install_options = install_options or []
        self.global_options = global_options or []
        self.hash_options = hash_options or {}

    def __str__(self) -> str:
        if self.req:
            s = str(self.req)
            if self.link:
                s += " from {}".format(self.link.url)
        elif self.link:
            s = self.link.url
        else:
            s = "<InstallRequirement>"
        if self.requirement_line:
            s += f" (from {self.requirement_line})"
        return s

    def __repr__(self) -> str:
        return "<{} object: {} is_editable={!r}>".format(
            self.__class__.__name__, str(self), self.is_editable
        )

    @property
    def name(self) -> Optional[str]:
        return self.req and self.req.name or None

    @property
    def specifier(self) -> SpecifierSet:
        return self.req and self.req.specifier or None

    @property
    def is_pinned(self) -> bool:
        """Return whether I am pinned to an exact version.

        For example, some-package==1.2 is pinned; some-package>1.2 is not.
        """
        specifiers = self.specifier
        return specifiers and len(specifiers) == 1 and next(iter(specifiers)).operator in {"==", "==="}

    def match_markers(self, extras_requested: Optional[Iterable[str]] = None) -> bool:
        if not extras_requested:
            # Provide an extra to safely evaluate the markers
            # without matching any extra
            extras_requested = ("",)
        if self.markers is not None:
            return any(
                self.markers.evaluate({"extra": extra}) for extra in extras_requested
            )
        else:
            return True

    def hashes(self) -> Hashes:
        """Return a hash-comparer that considers my option- and URL-based
        hashes to be known-good.

        Hashes in URLs--ones embedded in the requirements file, not ones
        downloaded from an index server--are almost peers with ones from
        flags. They satisfy --require-hashes (whether it was implicitly or
        explicitly activated) but do not activate it. md5 and sha224 are not
        allowed in flags, which should nudge people toward good algos. We
        always OR all hashes together, even ones from URLs.
        """
        good_hashes = self.hash_options.copy()
        link = self.link
        if link and link.hash:
            good_hashes.setdefault(link.hash_name, []).append(link.hash)
        return Hashes(good_hashes)

    @property
    def is_wheel(self) -> bool:
        if not self.link:
            return False
        return self.link.is_wheel

# PIPREQPARSE: end from src/pip/_internal/req/req_install.py
################################################################################

def specifier_key(spec):
    """
    A key function to sort packaging.Specifier
    """
    return spec.version, spec.operator


def ireq_to_dict(ireq: InstallRequirement) -> Dict:
    """
    Return a mapping of plain Pythomn object representing the ``ireq``
    InstallRequirement. 
    """
    if ireq.req:
        specifier = [
            str(s) 
            for s in sorted(ireq.specifier or [], key=specifier_key)
        ]
    else:
        specifier = []
    return dict(
        name=ireq.name,
        specifier=specifier,
        is_editable=ireq.is_editable,
        is_pinned= ireq.req and ireq.is_pinned or False,
        requirement_line=ireq.requirement_line.to_dict(),
        link=ireq.link and ireq.link.url or None,
        markers=ireq.markers and str(ireq.markers) or None,
        install_options=ireq.install_options,
        global_options=ireq.global_options,
        hash_options=ireq.hash_options or {},
        is_constraint=ireq.is_constraint,
        extras=ireq.extras and sorted(ireq.extras) or [],
    )

InstallRequirement.to_dict = ireq_to_dict

################################################################################
# PIPREQPARSE: from src/pip/_internal/vcs/versioncontrol.py

vcs_all_schemes = [
    'bzr+http', 'bzr+https', 'bzr+ssh', 'bzr+sftp', 'bzr+ftp', 'bzr+lp', 'bzr+file', 
    'git+http', 'git+https', 'git+ssh', 'git+git', 'git+file', 
    'hg+file', 'hg+http', 'hg+https', 'hg+ssh', 'hg+static-http', 
    'svn+ssh', 'svn+http', 'svn+https', 'svn+svn', 'svn+file',
]

vcs = ['ssh', 'git', 'hg', 'bzr', 'sftp', 'svn']


def is_url(name: str) -> bool:
    """
    Return true if the name looks like a URL.
    """
    scheme = get_url_scheme(name)
    if scheme is None:
        return False
    return scheme in ["http", "https", "file", "ftp"] + vcs_all_schemes

# PIPREQPARSE: end from src/pip/_internal/vcs/versioncontrol.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/utils/misc.py

NetlocTuple = Tuple[str, Tuple[Optional[str], Optional[str]]]


def read_chunks(file: BinaryIO, size: int = io.DEFAULT_BUFFER_SIZE) -> Iterator[bytes]:
    """Yield pieces of data from a file-like object until EOF."""
    while True:
        chunk = file.read(size)
        if not chunk:
            break
        yield chunk


def splitext(path: str) -> Tuple[str, str]:
    """Like os.path.splitext, but take off .tar too"""
    base, ext = posixpath.splitext(path)
    if base.lower().endswith(".tar"):
        ext = base[-4:] + ext
        base = base[:-4]
    return base, ext


def split_auth_from_netloc(netloc: str) -> NetlocTuple:
    """
    Parse out and remove the auth information from a netloc.

    Returns: (netloc, (username, password)).
    """
    if "@" not in netloc:
        return netloc, (None, None)

    # Split from the right because that's how urllib.parse.urlsplit()
    # behaves if more than one @ is present (which can be checked using
    # the password attribute of urlsplit()'s return value).
    auth, netloc = netloc.rsplit("@", 1)
    pw: Optional[str] = None
    if ":" in auth:
        # Split from the left because that's how urllib.parse.urlsplit()
        # behaves if more than one : is present (which again can be checked
        # using the password attribute of the return value)
        user, pw = auth.split(":", 1)
    else:
        user, pw = auth, None

    user = urllib.parse.unquote(user)
    if pw is not None:
        pw = urllib.parse.unquote(pw)

    return netloc, (user, pw)

# PIPREQPARSE: end from src/pip/_internal/utils/misc.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/utils/filetypes.py

WHEEL_EXTENSION = ".whl"
BZ2_EXTENSIONS: Tuple[str, ...] = (".tar.bz2", ".tbz")
XZ_EXTENSIONS: Tuple[str, ...] = (
    ".tar.xz",
    ".txz",
    ".tlz",
    ".tar.lz",
    ".tar.lzma",
)
ZIP_EXTENSIONS: Tuple[str, ...] = (".zip", WHEEL_EXTENSION)
TAR_EXTENSIONS: Tuple[str, ...] = (".tar.gz", ".tgz", ".tar")
ARCHIVE_EXTENSIONS = ZIP_EXTENSIONS + BZ2_EXTENSIONS + TAR_EXTENSIONS + XZ_EXTENSIONS


def is_archive_file(name: str) -> bool:
    """Return True if `name` is a considered as an archive file."""
    ext = splitext(name)[1].lower()
    if ext in ARCHIVE_EXTENSIONS:
        return True
    return False

# PIPREQPARSE: end from src/pip/_internal/utils/filetypes.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/req/constructors.py

logger = logging.getLogger(__name__)
operators = Specifier._operators.keys()


def _strip_extras(path: str) -> Tuple[str, Optional[str]]:
    m = re.match(r"^(.+)(\[[^\]]+\])$", path)
    extras = None
    if m:
        path_no_extras = m.group(1)
        extras = m.group(2)
    else:
        path_no_extras = path

    return path_no_extras, extras


def convert_extras(extras: Optional[str]) -> Set[str]:
    if not extras:
        return set()
    return Requirement("placeholder" + extras.lower()).extras


def parse_editable(editable_req: str) -> Tuple[Optional[str], str, Set[str]]:
    """Parses an editable requirement into:
        - a requirement name
        - an URL
        - extras
        - editable options
    Accepted requirements:
        svn+http://blahblah@rev#egg=Foobar[baz]&subdirectory=version_subdir
        .[some_extra]
    """

    url = editable_req

    # If a file path is specified with extras, strip off the extras.
    url_no_extras, extras = _strip_extras(url)

    if url_no_extras.lower().startswith(("file:", ".",)):
        package_name = Link(url_no_extras).egg_fragment
        if extras:
            return (
                package_name,
                url_no_extras,
                Requirement("placeholder" + extras.lower()).extras,
            )
        else:
            return package_name, url_no_extras, set()

    for version_control in vcs:
        if url.lower().startswith(f"{version_control}:"):
            url = f"{version_control}+{url}"
            break

    link = Link(url)

    if not link.is_vcs or not _looks_like_path(url):
        backends = ", ".join(vcs_all_schemes)
        raise InstallationError(
            f"{editable_req} is not a valid editable requirement. "
            f"It should either be a path to a local project or a VCS URL "
            f"(beginning with {backends})."
        )

    package_name = link.egg_fragment
    if not package_name:
        raise InstallationError(
            "Could not detect requirement name for '{}', please specify one "
            "with #egg=your_package_name".format(editable_req)
        )
    return package_name, url, set()


class RequirementParts:
    def __init__(
        self,
        requirement: Optional[Requirement],
        link: Optional[Link],
        markers: Optional[Marker],
        extras: Set[str],
    ):
        self.requirement = requirement
        self.link = link
        self.markers = markers
        self.extras = extras


def parse_req_from_editable(editable_req: str) -> RequirementParts:

    name, url, extras_override = parse_editable(editable_req)

    if name is not None:
        try:
            req: Optional[Requirement] = Requirement(name)
        except InvalidRequirement:
            raise InstallationError(f"Invalid requirement: '{name}'")
    else:
        req = None

    link = Link(url)

    return RequirementParts(
        requirement=req, 
        link=link, 
        markers=None, 
        extras=extras_override,
    )


# ---- The actual constructors follow ----


def install_req_from_editable(
    editable_req: str,
    requirement_line: Optional[RequirementLine] = None,
    options: Optional[Dict[str, Any]] = None,
    is_constraint: bool = False,
) -> InstallRequirement:

    parts = parse_req_from_editable(editable_req)

    return InstallRequirement(
        req=parts.requirement,
        requirement_line=requirement_line,
        is_editable=True,
        link=parts.link,
        is_constraint=is_constraint,
        install_options=options.get("install_options", []) if options else [],
        global_options=options.get("global_options", []) if options else [],
        hash_options=options.get("hashes", {}) if options else {},
        extras=parts.extras,
    )


def _looks_like_path(name: str) -> bool:
    """Checks whether the string "looks like" a path on the filesystem.

    This does not check whether the target actually exists, only judge from the
    appearance.

    Returns true if any of the following conditions is true:
    * a path separator is found (either os.path.sep or os.path.altsep);
    * a dot is found (which represents the current directory).
    """
    if os.path.sep in name:
        return True
    if os.path.altsep is not None and os.path.altsep in name:
        return True
    if name.startswith("."):
        return True
    return False


def _get_url_from_path(path: str, name: str) -> Optional[str]:
    """
    First, it checks whether a provided path is an installable directory. If it
    is, returns the path.

    If false, check if the path is an archive file (such as a .whl).
    The function checks if the path is a file. If false, if the path has
    an @, it will treat it as a PEP 440 URL requirement and return the path.
    """
    if _looks_like_path(name):
        return path

    if not is_archive_file(path):
        return None

    urlreq_parts = name.split("@", 1)
    if len(urlreq_parts) >= 2 and not _looks_like_path(urlreq_parts[0]):
        # If the path contains '@' and the part before it does not look
        # like a path, try to treat it as a PEP 440 URL req instead.
        return None
    return path


def parse_req_from_line(name: str) -> RequirementParts:
    """
    Return RequirementParts from a requirfement ``name`` string.
    Raise exceptions on error.
    """
    if is_url(name):
        marker_sep = "; "
    else:
        marker_sep = ";"
    if marker_sep in name:
        name, markers_as_string = name.split(marker_sep, 1)
        markers_as_string = markers_as_string.strip()
        if not markers_as_string:
            markers = None
        else:
            markers = Marker(markers_as_string)
    else:
        markers = None
    name = name.strip()
    req_as_string = None
    path = name
    link = None
    extras_as_string = None

    if is_url(name):
        link = Link(name)
    else:
        p, extras_as_string = _strip_extras(path)
        url = _get_url_from_path(p, name)
        if url is not None:
            link = Link(url)

    # it's a local file, dir, or url
    if link:
        # Handle relative file URLs
        if link.scheme == "file" and re.search(r"\.\./", link.url):
            link = Link(link.path)
        # wheel file
        if link.is_wheel:
            wheel = Wheel(link.filename)  # can raise InvalidWheelFilename
            req_as_string = f"{wheel.name}=={wheel.version}"
        else:
            # set the req to the egg fragment.  when it's not there, this
            # will become an 'unnamed' requirement
            req_as_string = link.egg_fragment

    # a requirement specifier
    else:
        req_as_string = name

    extras = convert_extras(extras_as_string)

    def _parse_req_string(req_as_string: str) -> Requirement:
        try:
            req = Requirement(req_as_string)
        except InvalidRequirement:
            if os.path.sep in req_as_string:
                add_msg = "It looks like a path."
                # add_msg += deduce_helpful_msg(req_as_string)
            elif "=" in req_as_string and not any(
                op in req_as_string for op in operators
            ):
                add_msg = "= is not a valid operator. Did you mean == ?"
            else:
                add_msg = ""
            msg = "Invalid requirement"
            if add_msg:
                msg += f"\nHint: {add_msg}"
            raise InstallationError(msg)
        else:
            # Deprecate extras after specifiers: "name>=1.0[extras]"
            # This currently works by accident because _strip_extras() parses
            # any extras in the end of the string and those are saved in
            # RequirementParts
            for spec in req.specifier:
                spec_str = str(spec)
                if spec_str.endswith("]"):
                    msg = f"Extras after version '{spec_str}'."
                    raise InstallationError(msg)
        return req

    if req_as_string is not None:
        req: Optional[Requirement] = _parse_req_string(req_as_string)
    else:
        req = None

    return RequirementParts(req, link, markers, extras)


def install_req_from_line(
    name: str,
    options: Optional[Dict[str, Any]] = None,
    is_constraint: bool = False,
    requirement_line: Optional[RequirementLine] = None,
) -> InstallRequirement:
    """Creates an InstallRequirement from a name, which might be a
    requirement, directory containing 'setup.py', filename, or URL.

    :param requirement_line: An optional RequirementLine describing where the
        line is from, for logging purposes in case of an error.
    """
    parts = parse_req_from_line(name=name)

    return InstallRequirement(
        req=parts.requirement,
        requirement_line=requirement_line,
        link=parts.link,
        markers=parts.markers,
        install_options=options.get("install_options", []) if options else [],
        global_options=options.get("global_options", []) if options else [],
        hash_options=options.get("hashes", {}) if options else {},
        is_constraint=is_constraint,
        extras=parts.extras,
    )


def install_req_from_parsed_requirement(
    parsed_req: ParsedRequirement,
) -> InstallRequirement:

    if parsed_req.is_editable:
        return install_req_from_editable(
            editable_req=parsed_req.requirement_string,
            is_constraint=parsed_req.is_constraint,
            requirement_line=parsed_req.requirement_line,
        )

    return install_req_from_line(
        name=parsed_req.requirement_string,
        options=parsed_req.options,
        is_constraint=parsed_req.is_constraint,
        requirement_line=parsed_req.requirement_line,
        )

# PIPREQPARSE: end from src/pip/_internal/req/constructors.py
################################################################################


################################################################################
# PIPREQPARSE: from src/pip/_internal/models/wheel.py

class Wheel:
    """A wheel file"""

    wheel_file_re = re.compile(
        r"""^(?P<namever>(?P<name>.+?)-(?P<ver>.*?))
        ((-(?P<build>\d[^-]*?))?-(?P<pyver>.+?)-(?P<abi>.+?)-(?P<plat>.+?)
        \.whl|\.dist-info)$""",
        re.VERBOSE,
    )

    def __init__(self, filename: str) -> None:
        """
        :raises InvalidWheelFilename: when the filename is invalid for a wheel
        """
        wheel_info = self.wheel_file_re.match(filename)
        if not wheel_info:
            raise InvalidWheelFilename(f"{filename} is not a valid wheel filename.")
        self.filename = filename
        self.name = wheel_info.group("name").replace("_", "-")
        # we'll assume "_" means "-" due to wheel naming scheme
        # (https://github.com/pypa/pip/issues/1150)
        self.version = wheel_info.group("ver").replace("_", "-")
        self.build_tag = wheel_info.group("build")
        self.pyversions = wheel_info.group("pyver").split(".")
        self.abis = wheel_info.group("abi").split(".")
        self.plats = wheel_info.group("plat").split(".")

        # All the tag combinations from this file
        self.file_tags = {
            Tag(x, y, z) for x in self.pyversions for y in self.abis for z in self.plats
        }

    def get_formatted_file_tags(self) -> List[str]:
        """Return the wheel's tags as a sorted list of strings."""
        return sorted(str(tag) for tag in self.file_tags)

    def support_index_min(self, tags: List[Tag]) -> int:
        """Return the lowest index that one of the wheel's file_tag combinations
        achieves in the given list of supported tags.

        For example, if there are 8 supported tags and one of the file tags
        is first in the list, then return 0.

        :param tags: the PEP 425 tags to check the wheel against, in order
            with most preferred first.

        :raises ValueError: If none of the wheel's file tags match one of
            the supported tags.
        """
        return min(tags.index(tag) for tag in self.file_tags if tag in tags)

    def find_most_preferred_tag(
        self, tags: List[Tag], tag_to_priority: Dict[Tag, int]
    ) -> int:
        """Return the priority of the most preferred tag that one of the wheel's file
        tag combinations achieves in the given list of supported tags using the given
        tag_to_priority mapping, where lower priorities are more-preferred.

        This is used in place of support_index_min in some cases in order to avoid
        an expensive linear scan of a large list of tags.

        :param tags: the PEP 425 tags to check the wheel against.
        :param tag_to_priority: a mapping from tag to priority of that tag, where
            lower is more preferred.

        :raises ValueError: If none of the wheel's file tags match one of
            the supported tags.
        """
        return min(
            tag_to_priority[tag] for tag in self.file_tags if tag in tag_to_priority
        )

    def supported(self, tags: Iterable[Tag]) -> bool:
        """Return whether the wheel is compatible with one of the given tags.

        :param tags: the PEP 425 tags to check the wheel against.
        """
        return not self.file_tags.isdisjoint(tags)

# PIPREQPARSE: end from src/pip/_internal/models/wheel.py
################################################################################
