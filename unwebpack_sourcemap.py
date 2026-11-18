#!/usr/bin/env python3
"""
    unwebpack_sourcemap.py
    by rarecoil (github.com/rarecoil/unwebpack-sourcemap)

    Reads Webpack source maps and extracts the disclosed
    uncompiled/commented source code for review. Can detect and
    attempt to read sourcemaps from Webpack bundles with the `-d`
    flag. Puts source into a directory structure similar to dev.
"""

import argparse
import base64
import json
import os
import re
import string
import sys
from urllib.parse import urlparse
from unicodedata import normalize

import requests
import urllib3
from bs4 import BeautifulSoup, SoupStrainer


# Matches both //# and //@ (legacy) sourceMappingURL comments
_SOURCEMAP_COMMENT_RE = re.compile(r"//[#@]\s*sourceMappingURL=(.+?)(?:\s*)$", re.MULTILINE)

# Webpack path prefixes to strip before writing
_WEBPACK_PATH_PREFIXES = re.compile(
    r"^webpack://"       # webpack://
    r"(?:[^/]*/)*"       # optional package-name segment(s), e.g. myapp/
)


class SourceMapExtractor(object):
    """Primary SourceMapExtractor class. Feed this arguments."""

    def __init__(self, options):
        """Initialize the class."""
        self._target = None
        self._is_local = False
        self._attempt_sourcemap_detection = False
        self._output_directory = ""
        self._target_extracted_sourcemaps = []
        self._path_sanitiser = None
        self._extracted_count = 0
        self._dangerously_write_paths = False

        if 'output_directory' not in options:
            raise SourceMapExtractorError("output_directory must be set in options.")

        self._output_directory = os.path.abspath(options['output_directory'])
        if not os.path.isdir(self._output_directory):
            if options.get('make_directory'):
                os.mkdir(self._output_directory)
            else:
                raise SourceMapExtractorError(
                    "output_directory does not exist. Pass --make-directory to auto-make it.")

        self._path_sanitiser = PathSanitiser(self._output_directory)

        self.disable_verify_ssl = options.get('disable_ssl_verification', False)
        self._is_local = options.get('local', False)
        self._attempt_sourcemap_detection = options.get('detect', False)
        self._dangerously_write_paths = options.get('dangerously_write_paths', False)
        self._timeout = options.get('timeout', 30)

        self._headers = {'User-Agent': 'Mozilla/5.0 (compatible; sourcemap-extractor/1.0)'}
        if options.get('cookies'):
            self._headers['Cookie'] = options['cookies']
        if options.get('auth_header'):
            self._headers['Authorization'] = options['auth_header']
        for header in options.get('header') or []:
            if ':' in header:
                name, _, value = header.partition(':')
                self._headers[name.strip()] = value.strip()

        if self.disable_verify_ssl:
            urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

        self._proxy = None
        if options.get('proxy'):
            self._proxy = {'http': options['proxy'], 'https': options['proxy']}

        self._validate_target(options['uri_or_file'])

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def run(self):
        """Run extraction process."""
        targets = self._collect_targets()
        for target in targets:
            if self._is_local:
                self._parse_sourcemap(target)
            elif self._attempt_sourcemap_detection:
                for sourcemap_uri in self._detect_js_sourcemaps(target):
                    self._parse_remote_sourcemap(sourcemap_uri)
            else:
                self._parse_remote_sourcemap(target)

        print("\nExtraction complete: %d file(s) written to %s" % (
            self._extracted_count, self._output_directory))

    # ------------------------------------------------------------------
    # Target collection
    # ------------------------------------------------------------------

    def _collect_targets(self):
        """Return a list of targets to process (supports --target-list)."""
        return [self._target]

    def _validate_target(self, target):
        """Do some basic validation on the target."""
        parsed = urlparse(target)
        if self._is_local:
            self._target = os.path.abspath(target)
            if not os.path.isfile(self._target):
                raise SourceMapExtractorError(
                    "uri_or_file is set to be a file, but doesn't seem to exist. check your path.")
        else:
            if parsed.scheme == "":
                raise SourceMapExtractorError(
                    "uri_or_file isn't a URI, and --local was not set. set --local?")
            self._target = target
            _, ext = os.path.splitext(parsed.path)
            if ext != '.map' and not self._attempt_sourcemap_detection:
                print("WARNING: URI does not have .map extension, and --detect is not flagged.")

    # ------------------------------------------------------------------
    # Remote helpers
    # ------------------------------------------------------------------

    def _parse_remote_sourcemap(self, uri):
        """GET a remote sourcemap and parse it."""
        data, final_uri = self._get_remote_data(uri)
        if data is not None:
            self._parse_sourcemap(data, is_str=True)
        else:
            print("WARNING: Could not retrieve sourcemap from URI %s" % final_uri)

    def _get_remote_data(self, uri, _redirect_count=0):
        """GET uri, manually following redirects (preserves URL for relative asset resolution)."""
        if _redirect_count > 10:
            print("WARNING: Too many redirects for URI %s" % uri)
            return None, uri

        try:
            result = requests.get(
                uri,
                verify=not self.disable_verify_ssl,
                headers=self._headers,
                proxies=self._proxy,
                timeout=self._timeout,
                allow_redirects=False,
            )
        except requests.exceptions.RequestException as e:
            print("WARNING: Request failed for URI %s: %s" % (uri, e))
            return None, uri

        if result.is_redirect:
            location = result.headers.get('Location', '')
            if not location:
                return None, uri
            if not urlparse(location).scheme:
                parsed = urlparse(uri)
                location = parsed.scheme + '://' + parsed.netloc + location
            return self._get_remote_data(location, _redirect_count + 1)

        if result.status_code == 200:
            return result.text, result.url

        print("WARNING: Got status code %d for URI %s" % (result.status_code, result.url))
        return None, result.url

    # ------------------------------------------------------------------
    # Detection (--detect mode)
    # ------------------------------------------------------------------

    def _detect_js_sourcemaps(self, uri):
        """Pull HTML and find sourceMappingURL references in JS assets, preload hints and HTML comments."""
        remote_sourcemaps = []
        data, final_uri = self._get_remote_data(uri)
        if data is None:
            print("WARNING: Could not retrieve HTML at %s" % uri)
            return remote_sourcemaps

        print("Detecting sourcemaps in HTML at %s" % final_uri)

        # Collect JS URLs from <script src> and <link rel="preload" as="script">
        js_urls = []
        try:
            script_strainer = SoupStrainer("script", src=True)
            soup_scripts = BeautifulSoup(data, "html.parser", parse_only=script_strainer)
            for tag in soup_scripts:
                js_urls.append(self._resolve_uri(tag['src'], final_uri))
        except Exception as e:
            raise SourceMapExtractorError("Could not parse HTML at URI %s: %s" % (final_uri, e))

        try:
            preload_strainer = SoupStrainer("link", rel=True)
            soup_preloads = BeautifulSoup(data, "html.parser", parse_only=preload_strainer)
            for tag in soup_preloads:
                rels = tag.get('rel', [])
                if isinstance(rels, str):
                    rels = [rels]
                if 'preload' in rels and tag.get('as') == 'script' and tag.get('href'):
                    js_urls.append(self._resolve_uri(tag['href'], final_uri))
        except Exception:
            pass  # preload scanning is best-effort

        # Also look for sourceMappingURL directly in HTML comments
        for match in _SOURCEMAP_COMMENT_RE.finditer(data):
            asset = match.group(1).strip()
            sm_uri = self._resolve_sourcemap_asset(asset, final_uri)
            if sm_uri and sm_uri not in remote_sourcemaps:
                print("Detected sourcemap in HTML comment: %s" % sm_uri)
                remote_sourcemaps.append(sm_uri)

        # Fetch each JS asset and scan for sourceMappingURL
        for js_url in js_urls:
            js_data, last_target_uri = self._get_remote_data(js_url)
            if js_data is None:
                print("WARNING: Could not fetch JS asset %s" % js_url)
                continue

            # Inline data-URI sourcemap (base64-encoded)
            inline = self._extract_inline_sourcemap(js_data)
            if inline:
                print("Detected inline (data-URI) sourcemap in %s" % js_url)
                self._parse_sourcemap(inline, is_str=True)
                continue

            for match in _SOURCEMAP_COMMENT_RE.finditer(js_data):
                asset = match.group(1).strip()
                sm_uri = self._resolve_sourcemap_asset(asset, last_target_uri)
                if sm_uri and sm_uri not in remote_sourcemaps:
                    print("Detected sourcemap at remote location %s" % sm_uri)
                    remote_sourcemaps.append(sm_uri)

        return remote_sourcemaps

    def _resolve_uri(self, src, base_uri):
        """Resolve a potentially relative src against base_uri."""
        parsed = urlparse(src)
        if parsed.scheme:
            return src
        base = urlparse(base_uri)
        return base.scheme + '://' + base.netloc + src

    def _resolve_sourcemap_asset(self, asset, base_uri):
        """Turn a sourceMappingURL value into an absolute URI, or return None for data URIs."""
        if asset.startswith('data:'):
            return None  # handled inline
        parsed = urlparse(asset)
        if parsed.scheme:
            return asset
        base = urlparse(base_uri)
        return (base.scheme + '://' + base.netloc +
                os.path.dirname(base.path) + '/' + asset)

    def _extract_inline_sourcemap(self, js_data):
        """Extract and decode a base64 data-URI sourcemap; returns JSON string or None."""
        for match in _SOURCEMAP_COMMENT_RE.finditer(js_data):
            asset = match.group(1).strip()
            if asset.startswith('data:application/json'):
                # data:application/json;base64,<payload>
                # data:application/json;charset=utf-8,<url-encoded JSON>
                if ';base64,' in asset:
                    try:
                        payload = asset.split(';base64,', 1)[1]
                        return base64.b64decode(payload).decode('utf-8', errors='ignore')
                    except Exception:
                        pass
                elif ',' in asset:
                    from urllib.parse import unquote
                    try:
                        return unquote(asset.split(',', 1)[1])
                    except Exception:
                        pass
        return None

    # ------------------------------------------------------------------
    # Sourcemap parsing
    # ------------------------------------------------------------------

    def _parse_sourcemap(self, target, is_str=False):
        if is_str:
            map_data = target
        else:
            if not os.path.isfile(target):
                print("ERROR: File not found: %s" % target)
                return False
            with open(target, 'r', encoding='utf-8', errors='ignore') as f:
                map_data = f.read()

        try:
            map_object = json.loads(map_data)
        except json.JSONDecodeError:
            print("ERROR: Failed to parse sourcemap. Are you sure this is a valid sourcemap?")
            return False

        if 'sources' not in map_object or 'sourcesContent' not in map_object:
            print("ERROR: Sourcemap does not contain sources and/or sourcesContent, cannot extract.")
            return False

        if len(map_object['sources']) != len(map_object['sourcesContent']):
            print("WARNING: sources != sourcesContent, filenames may not match content")

        for source, content in zip(map_object['sources'], map_object['sourcesContent']):
            if self._dangerously_write_paths:
                write_path = self._get_raw_file_path(source)
            else:
                write_path = self._get_sanitised_file_path(source)

            if write_path is None:
                print("ERROR: Could not resolve path for %s, skipping." % source)
                continue

            os.makedirs(os.path.dirname(write_path), mode=0o755, exist_ok=True)
            with open(write_path, 'w', encoding='utf-8', errors='ignore', newline='') as f:
                print("Writing %s..." % os.path.basename(write_path))
                f.write(content if content is not None else "")
                self._extracted_count += 1

    # ------------------------------------------------------------------
    # Path handling
    # ------------------------------------------------------------------

    def _strip_webpack_prefix(self, source_path):
        """Remove webpack:// and optional package-name prefix from a source path."""
        return _WEBPACK_PATH_PREFIXES.sub("", source_path)

    def _get_sanitised_file_path(self, source_path):
        """Sanitise a webpack source path for safe filesystem writing."""
        source_path = self._strip_webpack_prefix(source_path)
        parts = source_path.split(" ", 1)
        if parts[0] == "external":
            label = parts[1] if len(parts) > 1 else source_path
            print("WARNING: Found external sourcemap %s, not currently supported. Skipping." % label)
            return None

        path, filename = os.path.split(source_path)
        if path.startswith('./'):
            path = path[2:]
        if path.startswith('../'):
            path = 'parent_dir/' + path[3:]
        if path.startswith('.'):
            path = ""

        return self._path_sanitiser.make_valid_file_path(path, filename)

    def _get_raw_file_path(self, source_path):
        """
        Write the full path as declared in the sourcemap.
        Only used with --dangerously-write-paths; still confined to output_directory.
        """
        source_path = self._strip_webpack_prefix(source_path)
        # Strip leading slash so os.path.join doesn't treat it as absolute
        source_path = source_path.lstrip('/')
        full_path = os.path.abspath(os.path.join(self._output_directory, source_path))
        # Path traversal guard: must still be under output_directory
        if not full_path.startswith(os.path.abspath(self._output_directory) + os.sep):
            print("WARNING: Path %s would escape output directory, skipping." % source_path)
            return None
        return full_path


class PathSanitiser(object):
    """https://stackoverflow.com/questions/13939120/sanitizing-a-file-path-in-python"""

    EMPTY_NAME = "empty"

    def __init__(self, root_path):
        self.root_path = root_path
        self.empty_idx = 0

    def ensure_directory_exists(self, path_directory):
        if not os.path.exists(path_directory):
            os.makedirs(path_directory)

    def os_path_separators(self):
        seps = []
        for sep in (os.path.sep, os.path.altsep):
            if sep:
                seps.append(sep)
        return seps

    def sanitise_filesystem_name(self, potential_file_path_name):
        valid_filename = normalize('NFKD', potential_file_path_name).encode('ascii', 'ignore').decode('ascii')
        for sep in self.os_path_separators():
            valid_filename = valid_filename.replace(sep, '_')
        valid_chars = "-_.() {0}{1}".format(string.ascii_letters, string.digits)
        valid_filename = "".join(ch for ch in valid_filename if ch in valid_chars)
        test_chars = "{0}{1}".format(string.ascii_letters, string.digits)
        test_filename = "".join(ch for ch in potential_file_path_name if ch in test_chars)
        if len(test_filename) == 0:
            valid_filename = self.EMPTY_NAME + '_' + str(self.empty_idx)
            self.empty_idx += 1
        return valid_filename

    def get_root_path(self):
        filepath = os.path.abspath(self.root_path)
        if not any(filepath[-1] == sep for sep in self.os_path_separators()):
            filepath = '{0}{1}'.format(filepath, os.path.sep)
        self.ensure_directory_exists(filepath)
        return filepath

    def path_split_into_list(self, path):
        parts = []
        while True:
            newpath, tail = os.path.split(path)
            if newpath == path:
                assert not tail
                if path and path not in self.os_path_separators():
                    parts.append(path)
                break
            if tail and tail not in self.os_path_separators():
                parts.append(tail)
            path = newpath
        parts.reverse()
        return parts

    def sanitise_filesystem_path(self, potential_file_path):
        path_parts_list = self.path_split_into_list(potential_file_path)
        sanitised_path = ''
        for path_component in path_parts_list:
            sanitised_path = '{0}{1}{2}'.format(
                sanitised_path,
                self.sanitise_filesystem_name(path_component),
                os.path.sep)
        return sanitised_path

    def check_if_path_is_under(self, parent_path, child_path):
        child_parts = self.path_split_into_list(child_path)
        parent_parts = self.path_split_into_list(parent_path)
        if len(parent_parts) > len(child_parts):
            return False
        return all(p1 == p2 for p1, p2 in zip(child_parts, parent_parts))

    def make_valid_file_path(self, path=None, filename=None):
        root_path = self.get_root_path()
        if path:
            sanitised_path = self.sanitise_filesystem_path(path)
            if filename:
                sanitised_filename = self.sanitise_filesystem_name(filename)
                complete_path = os.path.join(root_path, sanitised_path, sanitised_filename)
            else:
                complete_path = os.path.join(root_path, sanitised_path)
        elif filename:
            sanitised_filename = self.sanitise_filesystem_name(filename)
            complete_path = os.path.join(root_path, sanitised_filename)
        else:
            return None  # nothing to write

        complete_path = os.path.abspath(complete_path)
        if self.check_if_path_is_under(root_path, complete_path):
            return complete_path
        return None


class SourceMapExtractorError(Exception):
    pass


if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="A tool to extract code from Webpack sourcemaps. Turns black boxes into gray ones.")
    parser.add_argument("-l", "--local", action="store_true", default=False,
        help="Target is a local file, not a URI.")
    parser.add_argument("-d", "--detect", action="store_true", default=False,
        help="Detect sourcemaps from <script src>, <link rel=preload as=script>, and HTML comments.")
    parser.add_argument("--make-directory", action="store_true", default=False,
        help="Create the output directory if it doesn't exist.")
    parser.add_argument("--dangerously-write-paths", action="store_true", default=False,
        help="Write original source paths instead of sanitising them. "
             "Still confined to the output directory. Use with caution on untrusted sources.")
    parser.add_argument("--disable-ssl-verification", action="store_true", default=False,
        help="Disable SSL certificate verification (useful for self-signed certs in internal apps).")
    parser.add_argument("--proxy",
        help="HTTP/HTTPS proxy URL, e.g. http://127.0.0.1:8080 to route through Burp Suite.")
    parser.add_argument("--cookies",
        help="Cookie header string to send with every request, e.g. 'session=abc123'.")
    parser.add_argument("--auth-header",
        help="Authorization header value, e.g. 'Bearer <token>'.")
    parser.add_argument("--header", metavar="NAME:VALUE", action="append",
        help="Extra request header (repeatable), e.g. --header 'X-Internal-Token: secret'.")
    parser.add_argument("--timeout", type=int, default=30,
        help="Per-request timeout in seconds (default: 30).")
    parser.add_argument("--target-list", metavar="FILE",
        help="Path to a file containing one URI or local path per line to process in batch.")

    parser.add_argument("uri_or_file",
        help="The target URI or local .map file. Ignored when --target-list is given.")
    parser.add_argument("output_directory",
        help="Directory to write extracted source files into.")

    if len(sys.argv) < 3:
        parser.print_usage()
        sys.exit(1)

    args = parser.parse_args()
    options = vars(args)

    # Batch mode: process every target from the list file
    if args.target_list:
        if not os.path.isfile(args.target_list):
            print("ERROR: --target-list file not found: %s" % args.target_list)
            sys.exit(1)
        with open(args.target_list, 'r', encoding='utf-8') as fh:
            targets = [line.strip() for line in fh if line.strip() and not line.startswith('#')]
        total_extracted = 0
        for target in targets:
            print("\n=== Processing %s ===" % target)
            opts = dict(options)
            opts['uri_or_file'] = target
            try:
                extractor = SourceMapExtractor(opts)
                extractor.run()
                total_extracted += extractor._extracted_count
            except SourceMapExtractorError as e:
                print("ERROR: %s" % e)
        print("\nBatch complete: %d total file(s) extracted." % total_extracted)
    else:
        extractor = SourceMapExtractor(options)
        extractor.run()
