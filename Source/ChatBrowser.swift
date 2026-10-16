import AppKit
import Combine
import CryptoKit
import SwiftUI
import WebKit

// MARK: - Result shape

/// A normalised tool result that crosses the JSONL bridge. The Python runtime
/// mirrors this on the worker side; the runtime forwards ``content`` to the
/// model verbatim, so all observation, navigation and screenshot data appear
/// as ``text`` blocks (or an ``image`` block, which the existing attachment
/// pipeline already understands).
struct ChatBrowserResult {
    var content: [[String: Any]]
    var isError: Bool
    var summary: String
    var changedFiles: [String]

    /// Build a successful result. ``details`` is appended to ``content`` as a
    /// second text block so the model can read snapshot ids, URLs and titles.
    /// ``image`` is forwarded as a content image block.
    static func ok(_ summary: String, _ text: String,
                   details: [String: Any] = [:],
                   image: [String: Any]? = nil) -> [String: Any] {
        var content: [[String: Any]] = [["type": "text", "text": text]]
        if !details.isEmpty,
           let data = try? JSONSerialization.data(withJSONObject: details,
                                                  options: [.sortedKeys]),
           let json = String(data: data, encoding: .utf8) {
            content.append(["type": "text", "text": json])
        }
        if let image { content.append(image) }
        return [
            "content": content,
            "is_error": false,
            "summary": summary,
            "changed_files": [],
        ]
    }

    static func failure(_ summary: String, _ text: String) -> [String: Any] {
        [
            "content": [["type": "text", "text": text]],
            "is_error": true,
            "summary": summary,
            "changed_files": [],
        ]
    }

    /// Build the standard ``image`` content block used by the attachment
    /// pipeline. The block format matches ``chat_attachments``'s source block.
    static func imageBlock(base64: String, mime: String,
                            width: Int, height: Int) -> [String: Any] {
        return [
            "type": "image",
            "source": [
                "type": "base64",
                "media_type": mime,
                "data": base64,
            ],
            "width": width,
            "height": height,
        ]
    }
}

// MARK: - Public protocol

/// Wire-level entry points. The host injects the store into the Python bridge
/// and calls ``perform(chatID:requestID:tool:arguments:)`` for every
/// ``native_action`` event, and ``cancel(chatID:requestID:)`` for every
/// ``native_cancel`` event. ``requestID`` is the opaque id the host wrote on
/// the wire so cancel targets exactly one in-flight call.
@MainActor
protocol ChatBrowserHost: AnyObject {
    func perform(chatID: String, requestID: String, tool: String,
                 arguments: [String: Any]) async -> [String: Any]
    func cancel(chatID: String, requestID: String)
    func webView(forChatID chatID: String) -> WKWebView?
    func close(chatID: String)
}

// MARK: - Page state

/// Public page metadata. ``navigationID`` increments on every committed
/// navigation and every provisional start, so any observe call made after a
/// navigation cannot reuse refs from the previous page generation.
struct ChatBrowserPage: Equatable {
    var title: String
    var url: String
    var navigationID: UInt64
    var snapshotID: String?
}

// MARK: - Session

/// Per-chat browser session. Owns one ``WKWebView``, runs the observation
/// script in a dedicated ``WKContentWorld`` so the page cannot tamper with the
/// snapshot functions or forge its own refs, and tracks navigation/snapshot
/// freshness for stale-ref rejection.
@MainActor
final class ChatBrowserSession: NSObject, ObservableObject,
                                WKNavigationDelegate, WKUIDelegate,
                                WKScriptMessageHandler {
    let chatID: String
    private let configuration: WKWebViewConfiguration
    private(set) var webView: WKWebView
    @Published private(set) var page: ChatBrowserPage
    @Published private(set) var isLoading: Bool = false
    private var pendingNavigation: CheckedContinuation<Bool, Never>?
    private var navigationToken: UUID?
    private var awaitedNavigation: WKNavigation?
    private var snapshotByNavigation: [UInt64: String] = [:]
    private let dataDirectory: URL
    /// Every isolated-world script (body, evaluateJavaScript, message handler)
    /// shares this single content world. Page-world JavaScript cannot reach
    /// it; isolated-world JavaScript cannot reach the page.
    private let isolatedWorld = WKContentWorld.world(name: "ChatBrowserIsolated")

    init(chatID: String, dataDirectory: URL) throws {
        self.chatID = chatID
        self.dataDirectory = dataDirectory
        let config = WKWebViewConfiguration()
        // Persistent per-chat profile. The chat id is a stable hex string;
        // derive a deterministic UUID from SHA-256 so each chat has its own
        // cookie jar / IndexedDB store inside the app sandbox.
        let digest = SHA256.hash(data: Data(chatID.utf8))
        let bytes = Array(digest)
        let uuid = UUID(uuid: (bytes[0], bytes[1], bytes[2], bytes[3],
                                bytes[4], bytes[5], bytes[6], bytes[7],
                                bytes[8], bytes[9], bytes[10], bytes[11],
                                bytes[12], bytes[13], bytes[14], bytes[15]))
        config.websiteDataStore = WKWebsiteDataStore(forIdentifier: uuid)
        let controller = WKUserContentController()
        config.userContentController = controller
        self.configuration = config
        self.webView = WKWebView(frame: .zero, configuration: config)
        self.page = ChatBrowserPage(title: "", url: "", navigationID: 0, snapshotID: nil)
        super.init()
        // The body script runs only in the isolated world. The page world
        // never sees it; the page cannot call our functions or fake refs.
        controller.addUserScript(WKUserScript(source: ChatBrowserObservationScript.body,
                                              injectionTime: .atDocumentStart,
                                              forMainFrameOnly: true,
                                              in: isolatedWorld))
        // The message handler is bound to the isolated world so the page
        // cannot post forged ``mutate``/``tagged`` messages back.
        controller.add(WeakScriptHandler(self),
                        contentWorld: isolatedWorld,
                        name: ChatBrowserObservationScript.koi)
        webView.navigationDelegate = self
        webView.uiDelegate = self
        try FileManager.default.createDirectory(at: dataDirectory,
                                                 withIntermediateDirectories: true)
    }

    // MARK: Navigation

    func open(url: URL) async -> Bool {
        await navigate(url: url)
    }

    private func navigate(url: URL) async -> Bool {
        guard !Task.isCancelled else { return false }
        guard let scheme = url.scheme?.lowercased(),
              ["http", "https"].contains(scheme) else { return false }
        isLoading = true
        let token = UUID()
        navigationToken = token
        let success: Bool = await withTaskCancellationHandler {
            await withCheckedContinuation { continuation in
                guard !Task.isCancelled else { continuation.resume(returning: false); return }
                pendingNavigation = continuation
                awaitedNavigation = webView.load(URLRequest(url: url))
            }
        } onCancel: {
            Task { @MainActor in
                guard self.navigationToken == token else { return }
                self.webView.stopLoading()
                let cont = self.pendingNavigation
                self.pendingNavigation = nil
                cont?.resume(returning: false)
            }
        }
        isLoading = false
        if navigationToken == token { navigationToken = nil; awaitedNavigation = nil }
        return success
    }

    // MARK: WKNavigationDelegate

    func webView(_ webView: WKWebView,
                 decidePolicyFor navigationAction: WKNavigationAction,
                 decisionHandler: @escaping (WKNavigationActionPolicy) -> Void) {
        // Scheme policy: only http(s) navigations are allowed. Blocks
        // javascript:, data:, file: and app: links, popups, form submits, and
        // any iframe / sub-frame load request that reaches the policy hook.
        if let url = navigationAction.request.url,
           let scheme = url.scheme?.lowercased(),
           ["http", "https"].contains(scheme) {
            decisionHandler(.allow)
        } else {
            decisionHandler(.cancel)
        }
    }

    func webView(_ webView: WKWebView,
                 createWebViewWith configuration: WKWebViewConfiguration,
                 for navigationAction: WKNavigationAction,
                 windowFeatures: WKWindowFeatures) -> WKWebView? {
        // No popups / new windows.
        return nil
    }

    func webView(_ webView: WKWebView,
                 didStartProvisionalNavigation navigation: WKNavigation!) {
        // The page is about to change. Invalidate every snapshot for the
        // previous generation so any in-flight ``act`` will see a stale
        // snapshot id when it resumes.
        snapshotByNavigation.removeAll(keepingCapacity: false)
        page.snapshotID = nil
        page.navigationID &+= 1
        isLoading = true
    }

    func webView(_ webView: WKWebView, didFinish navigation: WKNavigation!) {
        if pendingNavigation != nil && navigation !== awaitedNavigation { return }
        let title = webView.title ?? ""
        let url = webView.url?.absoluteString ?? ""
        let nextID = page.navigationID &+ 1
        page = ChatBrowserPage(title: title, url: url,
                               navigationID: nextID, snapshotID: nil)
        snapshotByNavigation.removeAll(keepingCapacity: false)
        isLoading = false
        pendingNavigation?.resume(returning: true)
        pendingNavigation = nil
    }

    func webView(_ webView: WKWebView, didFail navigation: WKNavigation!,
                 withError error: Error) {
        if navigation !== awaitedNavigation { return }
        isLoading = false
        pendingNavigation?.resume(returning: false)
        pendingNavigation = nil
    }

    func webView(_ webView: WKWebView, didFailProvisionalNavigation navigation: WKNavigation!,
                 withError error: Error) {
        if navigation !== awaitedNavigation { return }
        isLoading = false
        pendingNavigation?.resume(returning: false)
        pendingNavigation = nil
    }

    // MARK: WKUIDelegate (dialogs suppressed; page never opens one)

    func webView(_ webView: WKWebView,
                 runJavaScriptAlertPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping () -> Void) {
        completionHandler()
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptConfirmPanelWithMessage message: String,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (Bool) -> Void) {
        completionHandler(false)
    }

    func webView(_ webView: WKWebView,
                 runJavaScriptTextInputPanelWithPrompt prompt: String,
                 defaultText: String?,
                 initiatedByFrame frame: WKFrameInfo,
                 completionHandler: @escaping (String?) -> Void) {
        completionHandler(nil)
    }

    // MARK: Snapshot

    func observe(depth: Int, maxBytes: Int) async throws -> [String: Any] {
        // Cancellation must be honored at every observation entry point so
        // pending navigations and waits do not block a Stop.
        try Task.checkCancellation()
        let observedNavigationID = page.navigationID
        guard !isLoading else { return ChatBrowserResult.failure("Observe browser", "Navigation is in progress.") }
        let snapshotID = UUID().uuidString
        let js = ChatBrowserObservationScript.snapshotCall(
            maxBytes: maxBytes, depth: depth, snapshotID: snapshotID)
        let raw: Any?
        do { raw = try await evaluateIsolated(js) }
        catch is CancellationError { throw CancellationError() }
        catch { return ChatBrowserResult.failure("Observe browser",
                                                  "Observation failed: \(error.localizedDescription)") }
        try Task.checkCancellation()
        // Race: the page could have navigated while we were awaiting
        // ``evaluateJavaScript``. If it did, this snapshot is stale and we
        // do not record it.
        if page.navigationID != observedNavigationID {
            return ChatBrowserResult.failure("Observe browser",
                                             "The page navigated during observation. Re-run browser_observe.")
        }
        let decoded = (raw as? String).flatMap { $0.data(using: .utf8) }.flatMap { try? JSONSerialization.jsonObject(with: $0) }
        guard let json = decoded as? [String: Any], json["error"] == nil else {
            return ChatBrowserResult.failure("Observe browser",
                                             "Observation returned no JSON.")
        }
        snapshotByNavigation[page.navigationID] = snapshotID
        page.snapshotID = snapshotID
        var details: [String: Any] = [
            "snapshot_id": snapshotID,
            "url": page.url,
            "title": page.title,
        ]
        if let url = json["url"] as? String { details["page_url"] = url }
        if let title = json["title"] as? String { details["page_title"] = title }
        if let nodes = json["nodes"] { details["nodes"] = nodes }
        return ChatBrowserResult.ok("Observe browser",
                                     "Snapshot captured. Use the snapshot id with browser_act.",
                                     details: details)
    }

    func act(snapshotID: String, actions: [[String: Any]]) async throws -> [String: Any] {
        try Task.checkCancellation()
        guard let knownSnapshot = snapshotByNavigation.first(where: { $0.value == snapshotID }) else {
            return ChatBrowserResult.failure("Act on browser",
                                             "Snapshot id is stale or unknown. Re-run browser_observe before browser_act.")
        }
        let observedNavigationID = page.navigationID
        guard observedNavigationID == knownSnapshot.key else {
            return ChatBrowserResult.failure("Act on browser",
                                             "The page has navigated since this snapshot. Re-run browser_observe.")
        }
        var results: [[String: Any]] = []
        for (index, action) in actions.enumerated() {
            // Re-check cancellation and navigation generation after every
            // ``await``: an act in progress must abort if the page
            // navigates while we are typing into an element.
            try Task.checkCancellation()
            if page.navigationID != observedNavigationID {
                return ChatBrowserResult.failure("Act on browser",
                                                 "The page navigated during the action. Re-run browser_observe.")
            }
            let kind = (action["kind"] as? String) ?? ""
            switch kind {
            case "click", "focus", "hover":
                guard let ref = action["ref"] as? String else {
                    return ChatBrowserResult.failure("Act on browser",
                                                     "Action \(index) needs a fresh observation ref.")
                }
                let js = ChatBrowserObservationScript.actionCall(ref: ref, kind: kind)
                if (try? await evaluateIsolated(js)) as? Bool != true {
                    return ChatBrowserResult.failure("Act on browser",
                                                     "Action \(index) (\(kind)) failed: ref \(ref) not found.")
                }
                results.append(["index": index, "kind": kind, "ok": true])
            case "type":
                guard let ref = action["ref"] as? String,
                      let value = action["value"] as? String else {
                    return ChatBrowserResult.failure("Act on browser",
                                                     "Action \(index) needs a ref and a value.")
                }
                _ = try? await evaluateIsolated(
                    ChatBrowserObservationScript.actionCall(ref: ref, kind: "focus"))
                let setValue = ChatBrowserObservationScript.setValueCall(ref: ref, value: value)
                if (try? await evaluateIsolated(setValue)) as? Bool != true {
                    return ChatBrowserResult.failure("Act on browser",
                                                     "Action \(index) (type) failed: ref \(ref) not found.")
                }
                results.append(["index": index, "kind": "type", "ok": true])
            case "select":
                guard let ref = action["ref"] as? String,
                      let value = action["value"] as? String else {
                    return ChatBrowserResult.failure("Act on browser",
                                                     "Action \(index) needs a ref and a value.")
                }
                let js = ChatBrowserObservationScript.selectCall(ref: ref, value: value)
                if (try? await evaluateIsolated(js)) as? Bool != true {
                    return ChatBrowserResult.failure("Act on browser",
                                                     "Action \(index) (select) failed: ref \(ref) not found.")
                }
                results.append(["index": index, "kind": "select", "ok": true])
            case "scroll":
                let direction = (action["direction"] as? String) ?? "down"
                _ = try? await evaluateIsolated(
                    ChatBrowserObservationScript.scrollCall(direction: direction))
                results.append(["index": index, "kind": "scroll", "ok": true])
            case "press":
                guard let key = action["key"] as? String else {
                    return ChatBrowserResult.failure("Act on browser", "Action \(index) needs a key.")
                }
                _ = try? await evaluateIsolated(
                    ChatBrowserObservationScript.pressCall(key: key))
                results.append(["index": index, "kind": "press", "ok": true])
            case "wait_for_text":
                let timeout = (action["timeout_ms"] as? Int) ?? 5000
                guard let value = action["value"] as? String, !value.isEmpty else {
                    return ChatBrowserResult.failure("Act on browser", "Action \(index) needs a value.")
                }
                let found = await waitForText(value, timeout: timeout)
                results.append(["index": index, "kind": "wait_for_text", "found": found])
                if !found {
                    return ChatBrowserResult.failure("Act on browser",
                                                     "wait_for_text timed out: \(value)")
                }
            case "wait_for_selector":
                let timeout = (action["timeout_ms"] as? Int) ?? 5000
                guard let selector = action["selector"] as? String else {
                    return ChatBrowserResult.failure("Act on browser", "Action \(index) needs a selector.")
                }
                let found = await waitForSelector(selector, timeout: timeout)
                results.append(["index": index, "kind": "wait_for_selector", "found": found])
                if !found {
                    return ChatBrowserResult.failure("Act on browser",
                                                     "wait_for_selector timed out: \(selector)")
                }
            default:
                return ChatBrowserResult.failure("Act on browser",
                                                 "Action \(index) kind is not supported: \(kind)")
            }
        }
        return ChatBrowserResult.ok("Act on browser",
                                     "\(results.count) action(s) completed.",
                                     details: ["results": results])
    }

    private func evaluateIsolated(_ js: String) async throws -> Any? {
        try Task.checkCancellation()
        return try await withCheckedThrowingContinuation { continuation in
            webView.evaluateJavaScript(js, in: nil,
                                       in: isolatedWorld) { result in
                switch result {
                case .success(let value):
                    continuation.resume(returning: value)
                case .failure(let error):
                    continuation.resume(throwing: error)
                }
            }
        }
    }

    private func waitForText(_ value: String, timeout: Int) async -> Bool {
        let deadline = Date().addingTimeInterval(TimeInterval(timeout) / 1000.0)
        while Date() < deadline {
            if Task.isCancelled { return false }
            let js = "document.body && document.body.innerText && document.body.innerText.indexOf(\(escape(value))) >= 0"
            if let found = try? await evaluateIsolated(js) as? Bool, found {
                return true
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return false
    }

    private func waitForSelector(_ selector: String, timeout: Int) async -> Bool {
        let deadline = Date().addingTimeInterval(TimeInterval(timeout) / 1000.0)
        while Date() < deadline {
            if Task.isCancelled { return false }
            let js = "document.querySelector(\(escape(selector))) != null"
            if let found = try? await evaluateIsolated(js) as? Bool, found {
                return true
            }
            try? await Task.sleep(nanoseconds: 100_000_000)
        }
        return false
    }

    func screenshot(maxWidth: Int, maxHeight: Int) async throws -> [String: Any] {
        try Task.checkCancellation()
        let config = WKSnapshotConfiguration()
        config.afterScreenUpdates = true
        do {
            let image = try await webView.takeSnapshot(configuration: config)
            let scaled = downscale(image: image, maxWidth: maxWidth, maxHeight: maxHeight)
            guard let tiff = scaled.tiffRepresentation,
                  let rep = NSBitmapImageRep(data: tiff),
                  let data = rep.representation(using: .png, properties: [:]) else {
                return ChatBrowserResult.failure("Screenshot browser", "Could not encode PNG.")
            }
            let block = ChatBrowserResult.imageBlock(
                base64: data.base64EncodedString(),
                mime: "image/png",
                width: Int(scaled.size.width),
                height: Int(scaled.size.height))
            return ChatBrowserResult.ok("Screenshot browser",
                                         "Screenshot captured.",
                                         image: block)
        } catch {
            return ChatBrowserResult.failure("Screenshot browser", "Snapshot failed: \(error.localizedDescription)")
        }
    }

    private func downscale(image: NSImage, maxWidth: Int, maxHeight: Int) -> NSImage {
        let size = image.size
        guard size.width > 0, size.height > 0 else { return image }
        let scale = min(CGFloat(maxWidth) / size.width, CGFloat(maxHeight) / size.height, 1.0)
        if scale >= 1.0 { return image }
        let target = CGSize(width: size.width * scale, height: size.height * scale)
        let result = NSImage(size: target)
        result.lockFocus()
        NSGraphicsContext.current?.imageInterpolation = .high
        image.draw(in: NSRect(origin: .zero, size: target),
                   from: NSRect(origin: .zero, size: size),
                   operation: .copy, fraction: 1.0)
        result.unlockFocus()
        return result
    }

    // MARK: WKScriptMessageHandler

    func userContentController(_ userContentController: WKUserContentController,
                                didReceive message: WKScriptMessage) {
        // The injected script reports two kinds of events:
        // - "tagged" : page loaded and refs were tagged
        // - "mutate" : user focused/typed in an element, so the current
        //              snapshot is no longer trustworthy.
        guard message.name == ChatBrowserObservationScript.koi,
              let body = message.body as? [String: Any],
              let event = body["event"] as? String else { return }
        if event == "mutate" {
            page.snapshotID = nil
            snapshotByNavigation.removeValue(forKey: page.navigationID)
        }
    }

    // MARK: Cleanup

    func teardown() {
        webView.stopLoading()
        let cont = pendingNavigation
        pendingNavigation = nil
        cont?.resume(returning: false)
        let controller = webView.configuration.userContentController
        controller.removeScriptMessageHandler(forName: ChatBrowserObservationScript.koi,
                                             contentWorld: isolatedWorld)
        webView.navigationDelegate = nil
        webView.uiDelegate = nil
        page = ChatBrowserPage(title: "", url: "", navigationID: 0, snapshotID: nil)
        snapshotByNavigation.removeAll(keepingCapacity: false)
    }

    // MARK: JS escaping

    private func escape(_ string: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [string], options: .fragmentsAllowed)) ?? Data()
        if let s = String(data: data, encoding: .utf8), s.count >= 2 {
            return String(s.dropFirst().dropLast())
        }
        return ""
    }
}

/// WKScriptMessageHandler retains the handler; this wrapper keeps a weak
/// reference so the session can be deallocated when the chat closes.
private final class WeakScriptHandler: NSObject, WKScriptMessageHandler {
    weak var target: WKScriptMessageHandler?
    init(_ target: WKScriptMessageHandler) { self.target = target }
    func userContentController(_ userContentController: WKUserContentController,
                                didReceive message: WKScriptMessage) {
        target?.userContentController(userContentController, didReceive: message)
    }
}

// MARK: - Observation script

enum ChatBrowserObservationScript {
    static let koi = "chatBrowserObservation"

    /// Bundled observation script. Injected at document start in the isolated
    /// world so the page cannot tamper with it. The script tags interactive
    /// elements with a stable ``data-cb-ref`` and posts ``tagged``/``mutate``
    /// messages back to the host through the message handler.
    static let body: String = """
    (function() {
      if (window.__chatBrowserTagged === document) return;
      window.__chatBrowserTagged = document;
      window.__cbRefs = new Map();
      window.__cbElementRefs = new WeakMap();
      var interactive = ['a','button','input','textarea','select','option','label','summary','details'];
      var skip = ['script','style','noscript','meta','link','title','template'];
      function walk(node, seen, state) {
        if (!node || node.nodeType !== 1 || seen.has(node)) return;
        seen.add(node);
        var tag = (node.tagName || '').toLowerCase();
        if (skip.indexOf(tag) >= 0) return;
        if (interactive.indexOf(tag) >= 0 ||
            node.hasAttribute('onclick') ||
            node.getAttribute('tabindex') !== null ||
            node.getAttribute('role') === 'button') {
          state.idx += 1;
          var ref = 'r' + state.idx;
          window.__cbRefs.set(ref, node);
          window.__cbElementRefs.set(node, ref);
        }
        var children = node.childNodes;
        for (var i = 0; i < children.length; i++) walk(children[i], seen, state);
      }
      walk(document.documentElement, new WeakSet(), {idx: 0});
      try {
        if (window.webkit && window.webkit.messageHandlers.chatBrowserObservation) {
          window.webkit.messageHandlers.chatBrowserObservation.postMessage({event: 'tagged'});
        }
      } catch (e) {}
      function notify() {
        try {
          if (window.webkit && window.webkit.messageHandlers.chatBrowserObservation) {
            window.webkit.messageHandlers.chatBrowserObservation.postMessage({event: 'mutate'});
          }
        } catch (e) {}
      }
      document.addEventListener('focusin', notify, true);
      document.addEventListener('input', notify, true);
      document.addEventListener('change', notify, true);
    })();
    """

    static func snapshotCall(maxBytes: Int, depth: Int, snapshotID: String) -> String {
        return """
        (function() {
          try {
            var maxBytes = \(maxBytes);
            var depth = \(depth);
            var id = \(escape(snapshotID));
            window.__cbRefs = new Map();
            window.__cbElementRefs = new WeakMap();
            var nextRef = 0;
            function escapeText(value, limit) {
              if (!value) return '';
              if (value.length > limit) return value.slice(0, limit) + '…';
              return value;
            }
            function walk(node, level, out, bytes) {
              if (!node || node.nodeType !== 1 || bytes[0] >= maxBytes) return;
              if (level > depth) return;
              var tag = (node.tagName || '').toLowerCase();
              if (['script','style','noscript','meta','link','title','template'].indexOf(tag) >= 0) return;
              var ref = null;
              if (['a','button','input','textarea','select','option','label','summary','details'].indexOf(tag) >= 0 || node.hasAttribute('onclick') || node.hasAttribute('tabindex') || node.getAttribute('role') === 'button') {
                ref = 'r' + (++nextRef);
                window.__cbRefs.set(ref,node); window.__cbElementRefs.set(node,ref);
              }
              var attrs = {};
              for (var i = 0; i < node.attributes.length; i++) {
                var a = node.attributes[i];
                if (a.name.indexOf('on') === 0) continue;
                attrs[a.name] = escapeText(a.value, 80);
              }
              var text = '';
              for (var c = 0; c < node.childNodes.length; c++) {
                var ch = node.childNodes[c];
                if (ch.nodeType === 3) text += ch.nodeValue;
              }
              var entry = {
                tag: tag,
                ref: ref || null,
                role: node.getAttribute('role') || null,
                attrs: attrs,
                text: escapeText(text.trim(), 200)
              };
              out.push(entry);
              bytes[0] += JSON.stringify(entry).length;
              for (var j = 0; j < node.childNodes.length; j++) walk(node.childNodes[j], level + 1, out, bytes);
            }
            var out = []; var bytes = [0]; walk(document.documentElement, 0, out, bytes);
            return JSON.stringify({
              snapshot_id: id,
              url: location.href,
              title: document.title,
              bytes: bytes[0],
              truncated: bytes[0] >= maxBytes,
              nodes: out
            });
          } catch (error) {
            return JSON.stringify({error: error.toString()});
          }
        })()
        """
    }

    static func actionCall(ref: String, kind: String) -> String {
        return """
        (function() {
          var el = window.__cbRefs.get(\(escape(ref)));
          if (el && !el.isConnected) return false;
          if (!el) return false;
          var kind = "\(kind)";
          if (kind === 'click') el.click();
          else if (kind === 'focus') el.focus();
          else if (kind === 'hover') {
            var ev = new MouseEvent('mouseover', {bubbles: true});
            el.dispatchEvent(ev);
          }
          return true;
        })()
        """
    }

    static func setValueCall(ref: String, value: String) -> String {
        return """
        (function() {
          var el = window.__cbRefs.get(\(escape(ref)));
          if (el && !el.isConnected) return false;
          if (!el) return false;
          var tag = (el.tagName || '').toLowerCase();
          if (tag === 'input' || tag === 'textarea') {
            el.value = \(escape(value));
            el.dispatchEvent(new Event('input', {bubbles: true}));
            el.dispatchEvent(new Event('change', {bubbles: true}));
            return true;
          }
          el.textContent = \(escape(value));
          return true;
        })()
        """
    }

    static func selectCall(ref: String, value: String) -> String {
        return """
        (function() {
          var el = window.__cbRefs.get(\(escape(ref)));
          if (el && !el.isConnected) return false;
          if (!el || el.tagName.toLowerCase() !== 'select') return false;
          var value = \(escape(value));
          var found = false;
          for (var i = 0; i < el.options.length; i++) {
            if (el.options[i].value === value || el.options[i].text === value) {
              el.selectedIndex = i; found = true; break;
            }
          }
          if (found) el.dispatchEvent(new Event('change', {bubbles: true}));
          return found;
        })()
        """
    }

    static func scrollCall(direction: String) -> String {
        let dy: Int; let dx: Int
        switch direction {
        case "up": dy = -400; dx = 0
        case "left": dy = 0; dx = -400
        case "right": dy = 0; dx = 400
        default: dy = 400; dx = 0
        }
        return "window.scrollBy(\(dx), \(dy)); true"
    }

    static func pressCall(key: String) -> String {
        return """
        (function() {
          var ev = new KeyboardEvent('keydown', {key: \(escape(key)), bubbles: true});
          (document.activeElement || document.body).dispatchEvent(ev);
          return true;
        })()
        """
    }

    private static func escape(_ value: String) -> String {
        let data = (try? JSONSerialization.data(withJSONObject: [value], options: .fragmentsAllowed)) ?? Data()
        if let s = String(data: data, encoding: .utf8), s.count >= 2 {
            return String(s.dropFirst().dropLast())
        }
        return "\"\""
    }
}

// MARK: - Store

/// Owns one ``ChatBrowserSession`` per chat. Dispatches tool calls into the
/// right session and exposes a SwiftUI view model for the Browser pane.
///
/// Per-chat operations are serialised, but cancellation targets exactly the
/// task with the host-supplied request id so a new call started after cancel
/// is not affected.
@MainActor
final class ChatBrowserStore: ObservableObject, ChatBrowserHost {
    @Published private(set) var sessions: [String: ChatBrowserSession] = [:]
    let storageRoot: URL
    /// Inflight tasks keyed by ``chatID + "|" + requestID`` so cancellation
    /// can target the exact call without disturbing newer ones.
    private var inflight: [String: Task<[String: Any], Error>] = [:]
    /// Generation counter for every (chatID, requestID) key. ``Task`` is a
    /// value type and cannot be compared with ``===``; the counter is the
    /// stable identity we use to recognise "still our task" at cleanup time.
    private var generations: [String: Int] = [:]
    private var tails: [String: Task<[String: Any], Error>] = [:]
    private var tailKeys: [String: String] = [:]
    private var active: [String: String] = [:]
    private var observers: [String: AnyCancellable] = [:]

    init(storageRoot: URL) {
        self.storageRoot = storageRoot
    }

    private func session(for chatID: String) throws -> ChatBrowserSession {
        if let existing = sessions[chatID] { return existing }
        let dir = storageRoot.appendingPathComponent(chatID, isDirectory: true)
        let session = try ChatBrowserSession(chatID: chatID, dataDirectory: dir)
        sessions[chatID] = session
        observers[chatID] = session.objectWillChange.sink { [weak self] _ in
            self?.objectWillChange.send()
        }
        return session
    }

    func webView(forChatID chatID: String) -> WKWebView? {
        return sessions[chatID]?.webView
    }

    func perform(chatID: String, requestID: String, tool: String,
                 arguments: [String: Any]) async -> [String: Any] {
        // Empty request id is rejected up front so cancel cannot blow up the
        // host with an undefined key.
        guard !Task.isCancelled, !requestID.isEmpty else {
            return ChatBrowserResult.failure(tool, "Missing request id.")
        }
        let key = chatID + "|" + requestID
        guard inflight[key] == nil else { return ChatBrowserResult.failure(tool, "Duplicate request id.") }
        let gen = (generations[key] ?? 0) + 1
        generations[key] = gen
        let previous = tails[chatID]
        let task = Task<[String: Any], Error> { [weak self] in
            // All request IDs in one chat share the same serial chain.
            if let previous { _ = try? await previous.value }
            try Task.checkCancellation()
            guard let self else {
                return ChatBrowserResult.failure(tool, "Browser store is gone.")
            }
            self.active[chatID] = key
            defer { if self.active[chatID] == key { self.active[chatID] = nil } }
            return await self.doPerform(chatID: chatID, tool: tool, arguments: arguments)
        }
        inflight[key] = task
        tails[chatID] = task
        tailKeys[chatID] = key
        let result: [String: Any]
        do {
            result = try await withTaskCancellationHandler { try await task.value } onCancel: { task.cancel() }
        } catch is CancellationError {
            result = ChatBrowserResult.failure(tool, "Browser action cancelled.")
        } catch {
            result = ChatBrowserResult.failure(tool, "Browser action failed: \(error.localizedDescription)")
        }
        if generations[key] == gen {
            inflight.removeValue(forKey: key)
            generations.removeValue(forKey: key)
        }
        if tailKeys[chatID] == key { tails[chatID] = nil; tailKeys[chatID] = nil }
        return result
    }

    private func doPerform(chatID: String, tool: String,
                            arguments: [String: Any]) async -> [String: Any] {
        guard !Task.isCancelled else { return ChatBrowserResult.failure(tool, "Browser action cancelled.") }
        switch tool {
        case "browser_list":
            // List only the requested chat's page. ``browser_list`` must NOT
            // create a session — that would leak a blank WKWebView.
            guard let session = sessions[chatID] else {
                return ChatBrowserResult.ok("List browser sessions", "0 open session(s)",
                                             details: ["pages": []])
            }
            let pages: [[String: Any]] = [[
                "chat_id": session.chatID, "title": session.page.title,
                "url": session.page.url,
                "snapshot_id": session.page.snapshotID as Any,
                "navigation_id": session.page.navigationID,
            ]]
            return ChatBrowserResult.ok("List browser sessions", "1 open session(s)",
                                         details: ["pages": pages])
        case "browser_close":
            close(chatID: chatID)
            return ChatBrowserResult.ok("Close browser", "Browser closed.")
        default:
            break
        }
        // Tools that actually need a session. ``browser_open`` reuses an
        // existing session for the chat when one is already open.
        let session: ChatBrowserSession
        do { session = try self.session(for: chatID) }
        catch { return ChatBrowserResult.failure(tool, "Could not open browser session: \(error.localizedDescription)") }
        do {
            switch tool {
            case "browser_open":
                guard let urlString = arguments["url"] as? String,
                      let url = URL(string: urlString),
                      let scheme = url.scheme?.lowercased(),
                      ["http", "https"].contains(scheme) else {
                    return ChatBrowserResult.failure("Open browser", "Only http and https URLs are accepted.")
                }
                let ok = await session.open(url: url)
                if !ok {
                    return ChatBrowserResult.failure("Open browser", "The page failed to load.")
                }
                let page = session.page
                return ChatBrowserResult.ok("Open browser", "Page loaded.",
                                             details: ["url": page.url, "title": page.title,
                                                       "snapshot_id": page.snapshotID as Any,
                                                       "navigation_id": page.navigationID])
            case "browser_navigate":
                guard let urlString = arguments["url"] as? String,
                      let url = URL(string: urlString),
                      let scheme = url.scheme?.lowercased(),
                      ["http", "https"].contains(scheme) else {
                    return ChatBrowserResult.failure("Navigate browser", "Only http and https URLs are accepted.")
                }
                let ok = await session.open(url: url)
                if !ok {
                    return ChatBrowserResult.failure("Navigate browser", "The page failed to load.")
                }
                let page = session.page
                return ChatBrowserResult.ok("Navigate browser", "Page loaded.",
                                             details: ["url": page.url, "title": page.title,
                                                       "snapshot_id": page.snapshotID as Any,
                                                       "navigation_id": page.navigationID])
            case "browser_observe":
                let depth = (arguments["depth"] as? Int) ?? 3
                let maxBytes = (arguments["max_bytes"] as? Int) ?? 200_000
                return try await session.observe(depth: depth, maxBytes: maxBytes)
            case "browser_act":
                guard let snapshotID = arguments["snapshot_id"] as? String,
                      let actions = arguments["actions"] as? [[String: Any]] else {
                    return ChatBrowserResult.failure("Act on browser",
                                                     "browser_act needs snapshot_id and actions.")
                }
                return try await session.act(snapshotID: snapshotID, actions: actions)
            case "browser_screenshot":
                let maxWidth = (arguments["max_width"] as? Int) ?? 1280
                let maxHeight = (arguments["max_height"] as? Int) ?? 1280
                return try await session.screenshot(maxWidth: maxWidth, maxHeight: maxHeight)
            case "browser_remote_preview":
                let shot = try await session.screenshot(maxWidth: 640, maxHeight: 480)
                guard let blocks = shot["content"] as? [[String: Any]],
                      let block = blocks.first(where: { $0["type"] as? String == "image" }),
                      let source = block["source"] as? [String: Any], let base64 = source["data"] as? String,
                      let png = Data(base64Encoded: base64), let bitmap = NSBitmapImageRep(data: png) else { return shot }
                // Keep preview frames small enough for the existing encrypted
                // transport. It is the Mac's page, including its cookie jar.
                for quality in [0.55, 0.35, 0.2, 0.1] {
                    if let jpeg = bitmap.representation(using: .jpeg, properties: [.compressionFactor: quality]), jpeg.count <= 32_000 {
                        return ChatBrowserResult.ok("Mac browser preview", "Captured the shared page on your Mac.",
                            details: ["url": session.page.url, "title": session.page.title],
                            image: ChatBrowserResult.imageBlock(base64: jpeg.base64EncodedString(), mime: "image/jpeg",
                                width: block["width"] as? Int ?? 640, height: block["height"] as? Int ?? 480))
                    }
                }
                return ChatBrowserResult.failure("Mac browser preview", "This page is too detailed for a preview. Use Observe page to inspect its controls.")
            default:
                return ChatBrowserResult.failure(tool, "Unknown browser tool: \(tool)")
            }
        } catch is CancellationError {
            return ChatBrowserResult.failure(tool, "Browser action cancelled.")
        } catch {
            return ChatBrowserResult.failure(tool, "Browser action failed: \(error.localizedDescription)")
        }
    }

    func cancel(chatID: String, requestID: String) {
        // Target the exact (chatID, requestID) task. Anything else running
        // for this chat with a different id is left alone.
        guard !requestID.isEmpty else { return }
        let key = chatID + "|" + requestID
        guard let task = inflight[key] else { return }
        task.cancel()
        if active[chatID] == key { sessions[chatID]?.webView.stopLoading() }
    }

    func close(chatID: String) {
        sessions[chatID]?.teardown()
        sessions.removeValue(forKey: chatID)
        observers[chatID] = nil
        let prefix = chatID + "|"
        let keys = inflight.keys.filter { $0.hasPrefix(prefix) }
        for key in keys {
            inflight[key]?.cancel()
            inflight.removeValue(forKey: key)
        }
    }
}

// MARK: - SwiftUI pane

/// The Browser pane embeds the chat's persistent WKWebView. The store owns
/// the WKWebView; the pane is just the SwiftUI surface for navigation
/// controls and a frame. ``storageRoot`` is the app's private Application
/// Support directory; per-chat subdirectories are created inside it.
struct ChatBrowserPane: View {
    let chatID: String
    @ObservedObject var store: ChatBrowserStore
    @State private var inputURL: String = ""

    var body: some View {
        VStack(spacing: 0) {
            controls
            Divider()
            content
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity)
        .onAppear { inputURL = store.sessions[chatID]?.page.url ?? "" }
        .onChange(of: store.sessions[chatID]?.page.url) { _, new in
            inputURL = new ?? ""
        }
    }

    private var controls: some View {
        HStack(spacing: 8) {
            TextField("https://", text: $inputURL)
                .textFieldStyle(.roundedBorder)
                .onSubmit { Task { await navigate(inputURL) } }
            Button("Go") { Task { await navigate(inputURL) } }
            Button { Task { await back() } } label: { Image(systemName: "chevron.left") }
                .help("Back")
                .disabled(!(store.sessions[chatID]?.webView.canGoBack ?? false))
            Button { Task { await forward() } } label: { Image(systemName: "chevron.right") }
                .help("Forward")
                .disabled(!(store.sessions[chatID]?.webView.canGoForward ?? false))
            Button { Task { await reload() } } label: { Image(systemName: "arrow.clockwise") }
                .help("Reload")
            Button(role: .destructive) {
                store.close(chatID: chatID)
            } label: { Image(systemName: "xmark.circle") }
                .help("Close browser")
        }.padding(8)
    }

    @ViewBuilder
    private var content: some View {
        if let session = store.sessions[chatID] {
            BrowserWebContainer(webView: session.webView)
                .overlay(alignment: .topLeading) {
                    if session.isLoading {
                        ProgressView().controlSize(.small).padding(8)
                    }
                }
        } else {
            VStack(spacing: 12) {
                Image(systemName: "globe").font(.system(size: 32))
                    .foregroundStyle(.secondary)
                Text("Enter a URL to browse").font(.headline)
                Text("Type a URL above or call browser_open from the chat.")
                    .font(.subheadline).foregroundStyle(.secondary)
            }
            .frame(maxWidth: .infinity, maxHeight: .infinity)
        }
    }

    private func navigate(_ url: String) async {
        let trimmed = url.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty, let url = URL(string: trimmed) else { return }
        guard let scheme = url.scheme?.lowercased(),
              ["http", "https"].contains(scheme) else { return }
        // Manual navigation uses a synthetic request id; cancel is unlikely
        // here, but the wire stays consistent.
        _ = await store.perform(chatID: chatID, requestID: UUID().uuidString,
                                tool: "browser_open",
                                arguments: ["url": url.absoluteString])
    }

    private func back() async {
        guard let session = store.sessions[chatID] else { return }
        if session.webView.canGoBack { session.webView.goBack() }
    }

    private func forward() async {
        guard let session = store.sessions[chatID] else { return }
        if session.webView.canGoForward { session.webView.goForward() }
    }

    private func reload() async {
        guard let session = store.sessions[chatID] else { return }
        session.webView.reload()
    }
}

/// Hosts a WKWebView inside SwiftUI. WKWebView is an AppKit view; bridge it
/// through ``NSViewRepresentable``.
struct BrowserWebContainer: NSViewRepresentable {
    let webView: WKWebView
    func makeNSView(context: Context) -> WKWebView { webView }
    func updateNSView(_ nsView: WKWebView, context: Context) {}
}
