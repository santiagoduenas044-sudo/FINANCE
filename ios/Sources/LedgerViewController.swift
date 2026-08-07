import UIKit
import WebKit

/// Hosts the web app full-bleed, so the CSS `env(safe-area-inset-*)` rules
/// keep the tab bar clear of the home indicator and the header clear of the
/// notch — exactly as they do in Safari.
final class LedgerViewController: UIViewController {

    private var webView: WKWebView!
    private let schemeHandler = AppBundleSchemeHandler()

    private static let bridgeName = "ledger"

    override func loadView() {
        let configuration = WKWebViewConfiguration()
        configuration.setURLSchemeHandler(schemeHandler, forURLScheme: AppBundleSchemeHandler.scheme)
        configuration.websiteDataStore = .default()      // persists local storage between launches
        configuration.allowsInlineMediaPlayback = true
        configuration.suppressesIncrementalRendering = false

        let content = WKUserContentController()
        content.add(self, name: Self.bridgeName)
        configuration.userContentController = content

        let webView = WKWebView(frame: .zero, configuration: configuration)
        webView.scrollView.contentInsetAdjustmentBehavior = .never
        webView.scrollView.bounces = false
        webView.scrollView.showsVerticalScrollIndicator = false
        webView.allowsBackForwardNavigationGestures = false
        webView.isOpaque = false

        let background = UIColor(named: "LaunchBackground") ?? .systemBackground
        webView.backgroundColor = background
        webView.scrollView.backgroundColor = background

        if #available(iOS 16.4, *) {
            #if DEBUG
            webView.isInspectable = true
            #endif
        }

        self.webView = webView
        view = webView
    }

    override func viewDidLoad() {
        super.viewDidLoad()
        webView.load(URLRequest(url: AppBundleSchemeHandler.startURL))
    }

    override var preferredStatusBarStyle: UIStatusBarStyle { .default }
}

// MARK: - Saving a backup

extension LedgerViewController: WKScriptMessageHandler {

    /// The web app posts here instead of triggering a browser download, since
    /// an iOS app has no Downloads folder to write to.
    func userContentController(_ controller: WKUserContentController,
                               didReceive message: WKScriptMessage) {
        guard message.name == Self.bridgeName,
              let body = message.body as? [String: Any],
              body["action"] as? String == "save",
              let name = body["name"] as? String,
              let text = body["text"] as? String
        else { return }

        presentShareSheet(filename: name, contents: text)
    }

    private func presentShareSheet(filename: String, contents: String) {
        let safeName = filename.isEmpty ? "ledger-backup.json" : filename
        let url = FileManager.default.temporaryDirectory.appendingPathComponent(safeName)

        do {
            try contents.write(to: url, atomically: true, encoding: .utf8)
        } catch {
            return
        }

        let sheet = UIActivityViewController(activityItems: [url], applicationActivities: nil)
        sheet.popoverPresentationController?.sourceView = view
        sheet.popoverPresentationController?.sourceRect = CGRect(
            x: view.bounds.midX, y: view.bounds.midY, width: 1, height: 1
        )
        present(sheet, animated: true)
    }
}
