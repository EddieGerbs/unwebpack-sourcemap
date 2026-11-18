# unwebpack-sourcemap

Recover uncompiled TypeScript sources, JSX, and more from Webpack sourcemaps — for use in penetration testing and static code analysis.

As single-page applications take over the world, more and more is being asked of the browser as a client. It is common for SPAs to use [Webpack](https://webpack.js.org/) to handle browser script build processes. Usually, Webpack will transpile React/Vue/TypeScript/etc. to JavaScript, minify/compress it, and then serve it as a single bundle to the application.

However, Webpack also produces [JavaScript source maps](https://www.html5rocks.com/en/tutorials/developertools/sourcemaps/) to assist in the debugging and development process; when things go wrong, the browser's debugger can use the SourceMap to point to a line in the code that contains the issue at hand. Most developers do not adequately protect the source maps and ship them to production environments.

When the browser was simply handling an array of JavaScript files concatenated and (maybe) packed, this wasn't so much of an issue. However, developers of SPAs assume the use of JavaScript as an **intermediate representation**. Developers often expect production to contain obfuscated and/or otherwise-processed scripts, and do not understand just what the sourcemaps contain in many cases. This model aligns closely with shipping binaries: source is compiled and you ship the interpretable version. If this is the case, the sourcemap is akin to leaking your source alongside the "binary" (bundle) you have made. The bundle can be reverse engineered just as a binary can, but sourcemaps make this far easier.


## Requirements

Python 3.8+ with `beautifulsoup4` and `requests`. Install with:

```
pip3 install -r requirements.txt
```


## Usage

In all examples below, `output` is the directory where extracted source files will be written.

```
mkdir output
```

Unpack a **local** sourcemap:

```
./unwebpack_sourcemap.py --local /path/to/source.map output
```

Unpack a **remote** sourcemap:

```
./unwebpack_sourcemap.py https://pathto.example.com/source.map output
```

**Detect** and pull all sourcemaps linked from an HTML page (`<script src>`, `<link rel="preload" as="script">`, and inline HTML comments):

```
./unwebpack_sourcemap.py --detect https://pathto.example.com/spa_root/ output
```

**Batch mode** — process many targets at once from a file (one URI or path per line, `#` lines are comments):

```
./unwebpack_sourcemap.py --target-list targets.txt output
```

### Pentest-specific options

Route traffic through an intercepting proxy (e.g. Burp Suite):

```
./unwebpack_sourcemap.py --proxy http://127.0.0.1:8080 --disable-ssl-verification \
    https://pathto.example.com/source.map output
```

Pass session cookies to access authenticated resources:

```
./unwebpack_sourcemap.py --cookies "session=abc123; csrf=xyz" \
    https://pathto.example.com/source.map output
```

Pass an Authorization header (e.g. Bearer token):

```
./unwebpack_sourcemap.py --auth-header "Bearer eyJ..." \
    https://pathto.example.com/source.map output
```

Pass arbitrary custom headers (repeatable):

```
./unwebpack_sourcemap.py --header "X-Internal-Token: secret" \
                         --header "X-Forwarded-For: 127.0.0.1" \
    https://pathto.example.com/source.map output
```

Write original (unsanitised) source paths from the sourcemap:

```
./unwebpack_sourcemap.py --dangerously-write-paths \
    https://pathto.example.com/source.map output
```

> **Warning:** `--dangerously-write-paths` writes the directory structure exactly as declared in the sourcemap (after stripping the `webpack://` prefix). Paths are still confined to the output directory — traversal attempts are blocked — but be aware of what you are writing from an untrusted source.


### Full options reference

| Flag | Description |
|---|---|
| `-l`, `--local` | Target is a local `.map` file, not a URI |
| `-d`, `--detect` | Scan `<script src>`, `<link rel="preload" as="script">` and HTML comments on the page, fetch each JS asset, and extract any linked or inline sourcemaps |
| `--make-directory` | Create the output directory if it does not exist |
| `--dangerously-write-paths` | Write original source paths instead of sanitising them (still confined to output directory) |
| `--disable-ssl-verification` | Skip TLS certificate verification |
| `--proxy URL` | HTTP/HTTPS proxy, e.g. `http://127.0.0.1:8080` |
| `--cookies VALUE` | Cookie header string sent with every request |
| `--auth-header VALUE` | `Authorization` header value sent with every request |
| `--header NAME:VALUE` | Arbitrary extra header (repeatable) |
| `--timeout N` | Per-request timeout in seconds (default: 30) |
| `--target-list FILE` | Batch mode: file of URIs/paths, one per line |


## I'm a developer and this scares me. What do?

You have a few options:

1. Turn off sourcemaps in production entirely.
1. Push sourcemaps to a private server, and ACL sourcemap URIs to developers only.
1. Load sourcemaps from local sources only and do not push them to production.


## Example Vulnerable Application

An example TypeScript+React application is included in `example-react-ts-app`. You can run this locally and run the script against it.


## License

MIT.
