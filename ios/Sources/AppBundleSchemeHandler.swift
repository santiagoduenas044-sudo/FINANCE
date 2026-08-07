import Foundation
import WebKit

/// Serves the bundled web app over a private URL scheme.
///
/// Loading from `file://` would be simpler, but WebKit gives file URLs an
/// opaque origin and denies them local storage — which is where every figure
/// in this app lives. A custom scheme gets a real, stable origin
/// (`ledger://app`), so saved data survives relaunches.
final class AppBundleSchemeHandler: NSObject, WKURLSchemeHandler {

    static let scheme = "ledger"
    static let startURL = URL(string: "ledger://app/index.html")!

    /// The `www` folder reference copied into the app bundle at build time.
    private let root: URL? = Bundle.main.url(forResource: "www", withExtension: nil)

    func webView(_ webView: WKWebView, start task: WKURLSchemeTask) {
        guard let url = task.request.url, let root = root else {
            task.didFailWithError(URLError(.fileDoesNotExist))
            return
        }

        var path = url.path
        if path.isEmpty || path == "/" { path = "/index.html" }

        let file = root.appendingPathComponent(String(path.dropFirst())).standardizedFileURL

        // Never serve anything outside the bundled folder.
        guard file.path.hasPrefix(root.standardizedFileURL.path),
              let data = try? Data(contentsOf: file) else {
            task.didFailWithError(URLError(.fileDoesNotExist))
            return
        }

        guard let response = HTTPURLResponse(
            url: url,
            statusCode: 200,
            httpVersion: "HTTP/1.1",
            headerFields: [
                "Content-Type": Self.mimeType(for: file.pathExtension),
                "Content-Length": String(data.count),
                "Cache-Control": "no-store",
            ]
        ) else {
            task.didFailWithError(URLError(.badServerResponse))
            return
        }

        task.didReceive(response)
        task.didReceive(data)
        task.didFinish()
    }

    func webView(_ webView: WKWebView, stop task: WKURLSchemeTask) {
        // Every response is delivered synchronously in `start`, so there is
        // nothing in flight to cancel.
    }

    private static func mimeType(for pathExtension: String) -> String {
        switch pathExtension.lowercased() {
        case "html", "htm":         return "text/html; charset=utf-8"
        case "js", "mjs":           return "text/javascript; charset=utf-8"
        case "css":                 return "text/css; charset=utf-8"
        case "json", "webmanifest": return "application/json; charset=utf-8"
        case "svg":                 return "image/svg+xml"
        case "png":                 return "image/png"
        case "jpg", "jpeg":         return "image/jpeg"
        case "woff2":               return "font/woff2"
        case "woff":                return "font/woff"
        case "ico":                 return "image/x-icon"
        default:                    return "application/octet-stream"
        }
    }
}
