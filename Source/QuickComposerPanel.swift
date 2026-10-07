import AppKit
import SwiftUI

// The recent-chat quick composer as a floating window of Provider Hub's own:
// glass like the compact shell, movable to any display, over other apps when
// pinned. The codex-accent helper feeds it rows and previews and performs
// each send; this file only shows what the helper reports and asks it to
// send what the user typed (see codex_quick_host.py for the channel).

struct QuickRow: Identifiable, Equatable {
    let threadId: String
    let hostId: String
    let kind: String
    let title: String
    let supported: Bool
    let active: Bool
    let activeAccent: String?
    let preview: String?
    var id: String { kind + "|" + hostId + "|" + threadId }

    init?(_ object: [String: Any]) {
        guard let threadId = object["threadId"] as? String, let hostId = object["hostId"] as? String,
              let kind = object["kind"] as? String else { return nil }
        self.threadId = threadId; self.hostId = hostId; self.kind = kind
        let title = object["title"] as? String
        self.title = (title?.isEmpty == false) ? title! : "Untitled chat"
        supported = object["supported"] as? Bool == true
        active = object["active"] as? Bool == true
        activeAccent = object["activeAccent"] as? String
        let preview = object["preview"] as? String
        self.preview = (preview?.isEmpty == false) ? preview : nil
    }
}

@MainActor
final class QuickPanelModel: ObservableObject {
    @Published var rows: [QuickRow] = []
    @Published var selected: String? {
        didSet {
            // Each chat keeps its own draft; the text field only ever binds
            // to `draft`, so row updates never rebuild it mid-keystroke.
            if let oldValue, oldValue != selected { drafts[oldValue] = draft }
            draft = selected.flatMap { drafts[$0] } ?? ""
        }
    }
    @Published var draft = ""
    private var drafts: [String: String] = [:]
    @Published var statuses: [String: String] = [:]
    @Published var pending: Set<String> = []
    @Published var helperConnected = false
    @Published var pinned = true { didSet { panel?.level = pinned ? .floating : .normal } }
    /// Writes one JSON line to the helper's stdin; set by the model that owns the helper.
    var send: ((String) -> Void)?
    private var requests: [String: String] = [:]
    private var sentPrompts: [String: String] = [:]
    private(set) var panel: QuickComposerPanel?
    private(set) var visible = false

    var selectedRow: QuickRow? { rows.first { $0.id == selected } }

    // MARK: Events from the helper

    func handle(event: [String: Any]) {
        switch event["event"] as? String {
        case "host":
            helperConnected = event["connected"] as? Bool == true
            if helperConnected && visible { command(["command": "quick-watch", "active": true]) }
        case "quick-open":
            show()
        case "quick-rows":
            let incoming = (event["rows"] as? [[String: Any]] ?? []).prefix(10).compactMap(QuickRow.init)
            var seen = Set<String>()
            let next = incoming.filter { seen.insert($0.id).inserted }
            // Previews change while a thread streams; only a real change
            // re-renders the list, and the selection and draft are untouched.
            if next != rows { rows = next }
            if selectedRow == nil || selectedRow?.supported == false { selected = rows.first { $0.supported }?.id }
        case "quick-result":
            guard let requestId = event["requestId"] as? String, let id = requests.removeValue(forKey: requestId) else { return }
            pending.remove(id)
            let ok = event["ok"] as? Bool == true
            if ok {
                if id == selected, draft == sentPrompts[id] { draft = "" }
                else if drafts[id] == sentPrompts[id] { drafts[id] = "" }
            }
            sentPrompts[id] = nil
            statuses[id] = (event["reason"] as? String).flatMap { $0.isEmpty ? nil : $0 }
                ?? (ok ? "Sent." : "Send failed. Your draft is kept.")
        default:
            break
        }
    }

    func helperGone() {
        helperConnected = false
        rows = []
        pending = []
        requests = [:]
    }

    // MARK: Commands to the helper

    private func command(_ object: [String: Any]) {
        guard let data = try? JSONSerialization.data(withJSONObject: object),
              let line = String(data: data, encoding: .utf8) else { return }
        send?(line + "\n")
    }

    var status: String {
        guard let row = selectedRow else { return rows.isEmpty ? "" : "Select a local Codex chat." }
        if !row.supported { return "Remote and ChatGPT chats are unavailable here." }
        return statuses[row.id] ?? ""
    }

    var canSend: Bool {
        guard let row = selectedRow, row.supported, helperConnected else { return false }
        return !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty && !pending.contains(row.id)
    }

    func submit() {
        guard canSend, let row = selectedRow else { return }
        let prompt = draft
        let requestId = UUID().uuidString
        requests[requestId] = row.id
        sentPrompts[row.id] = prompt
        pending.insert(row.id)
        statuses[row.id] = "Sending…"
        command(["command": "quick-send", "requestId": requestId, "threadId": row.threadId, "prompt": prompt])
    }

    // MARK: The window

    func show() {
        let panel = self.panel ?? makePanel()
        if visible && panel.isKeyWindow { return }  // a repeated request must not disturb typing
        let wasVisible = visible
        visible = true
        panel.makeKeyAndOrderFront(nil)
        NSApp.activate(ignoringOtherApps: true)
        if !wasVisible { command(["command": "quick-watch", "active": true]) }
    }

    func hide() {
        panel?.orderOut(nil)
        panelDidClose()
    }

    fileprivate func panelDidClose() {
        guard visible else { return }
        visible = false
        command(["command": "quick-watch", "active": false])
    }

    private func makePanel() -> QuickComposerPanel {
        let panel = QuickComposerPanel(contentRect: NSRect(x: 0, y: 0, width: 420, height: 560),
                                       styleMask: [.titled, .closable, .resizable, .fullSizeContentView],
                                       backing: .buffered, defer: false)
        panel.model = self
        panel.title = "Recent chats"
        panel.titleVisibility = .hidden
        panel.titlebarAppearsTransparent = true
        panel.isMovableByWindowBackground = true
        panel.isOpaque = false
        panel.backgroundColor = .clear
        panel.hasShadow = true
        panel.hidesOnDeactivate = false
        panel.isReleasedWhenClosed = false
        panel.level = pinned ? .floating : .normal
        panel.collectionBehavior = [.canJoinAllSpaces, .fullScreenAuxiliary]
        panel.minSize = NSSize(width: 340, height: 380)
        panel.contentView = NSHostingView(rootView: QuickComposerView(model: self))
        // With full-size content the hosting view can end up above the title
        // bar's container, hiding the traffic lights; keep that container on top.
        if let close = panel.standardWindowButton(.closeButton), let container = close.superview?.superview,
           let frame = container.superview {
            frame.addSubview(container, positioned: .above, relativeTo: nil)
        }
        // Reopen where it was left; the first time, in the middle of the screen.
        if !panel.setFrameUsingName("ProviderHubQuickComposer") { panel.center() }
        panel.setFrameAutosaveName("ProviderHubQuickComposer")
        self.panel = panel
        return panel
    }
}

final class QuickComposerPanel: NSPanel {
    weak var model: QuickPanelModel?
    override var canBecomeKey: Bool { true }
    override var canBecomeMain: Bool { false }
    override func close() {
        super.close()
        model?.panelDidClose()
    }
    override func cancelOperation(_ sender: Any?) { close() }
}

private typealias Semantic = HubTheme.Semantic

struct QuickComposerView: View {
    @ObservedObject var model: QuickPanelModel
    @FocusState private var composing: Bool

    var body: some View {
        VStack(spacing: 0) {
            HStack(spacing: 8) {
                Text("Recent chats").font(.system(size: 13, weight: .semibold)).foregroundStyle(Semantic.ink)
                Spacer()
                if !model.helperConnected {
                    Text("Codex helper not running").font(.system(size: 11)).foregroundStyle(Semantic.secondaryInk)
                }
                Button { model.pinned.toggle() } label: {
                    Image(systemName: model.pinned ? "pin.fill" : "pin").font(.system(size: 11, weight: .semibold))
                        .foregroundStyle(model.pinned ? Semantic.accentOnSurface : Semantic.secondaryInk)
                }
                .buttonStyle(.plain).help(model.pinned ? "Stays over other windows" : "Behaves like a normal window")
            }
            .padding(.leading, 76).padding(.trailing, 14).padding(.top, 9).padding(.bottom, 6)
            ScrollView {
                LazyVStack(spacing: 2) {
                    if model.rows.isEmpty {
                        Text(model.helperConnected ? "No recent chats are available in the sidebar." : "Launch Codex / ChatGPT from Provider Hub to see recent chats.")
                            .font(.system(size: 13)).foregroundStyle(Semantic.secondaryInk)
                            .frame(maxWidth: .infinity).padding(.vertical, 22).padding(.horizontal, 12)
                    }
                    ForEach(model.rows) { row in
                        QuickRowView(row: row, selected: row.id == model.selected) {
                            guard row.supported else { return }
                            model.selected = row.id
                            composing = true
                        }
                    }
                }
                .padding(.horizontal, 8).padding(.vertical, 4)
            }
            HStack(spacing: 8) {
                TextField(placeholder, text: $model.draft)
                    .textFieldStyle(.plain).font(.system(size: 14)).foregroundStyle(Semantic.ink)
                    .focused($composing).disabled(model.selectedRow?.supported != true)
                    .onSubmit { model.submit() }
                Button { model.submit() } label: {
                    Image(systemName: "arrow.up").font(.system(size: 13, weight: .bold))
                        .foregroundStyle(Semantic.inkOnAccent).frame(width: 28, height: 28)
                        .background(Circle().fill(Semantic.accentOnSurface))
                }
                .buttonStyle(.plain).disabled(!model.canSend).opacity(model.canSend ? 1 : 0.35)
            }
            .padding(.leading, 16).padding(.trailing, 6).padding(.vertical, 6)
            .background(Capsule().fill(Semantic.raisedSurface))
            .overlay(Capsule().stroke(Semantic.hairline, lineWidth: 1))
            .padding(.horizontal, 12).padding(.top, 8)
            Text(model.status).font(.system(size: 11)).foregroundStyle(Semantic.secondaryInk)
                .lineLimit(2).frame(maxWidth: .infinity, alignment: .leading)
                .frame(minHeight: 24).padding(.horizontal, 18).padding(.bottom, 8)
        }
        .background(VibrancyBackground())
        // The hosting view insets content below the hidden title bar; the
        // glass has to reach the traffic lights, so the panel owns that band.
        .ignoresSafeArea()
        .onExitCommand { model.hide() }
    }

    private var placeholder: String {
        guard let row = model.selectedRow, row.supported else { return "Select a local Codex chat" }
        return "Message " + row.title + "…"
    }
}

private struct QuickRowView: View {
    let row: QuickRow
    let selected: Bool
    let choose: () -> Void

    var body: some View {
        Button(action: choose) {
            HStack(spacing: 8) {
                VStack(alignment: .leading, spacing: 1) {
                    Text(row.title).font(.system(size: 13)).foregroundStyle(Semantic.ink).lineLimit(1)
                    Text(row.supported ? (row.preview ?? "No response preview available") : "Unavailable in this window")
                        .font(.system(size: 12)).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
                }
                Spacer(minLength: 0)
                if row.active {
                    ProgressView().controlSize(.small).tint(accent)
                }
            }
            .padding(.horizontal, 10).padding(.vertical, 7)
            .background(RoundedRectangle(cornerRadius: 10).fill(selected ? Semantic.selection : Color.clear))
            .contentShape(RoundedRectangle(cornerRadius: 10))
        }
        .buttonStyle(.plain).opacity(row.supported ? 1 : 0.45)
    }

    private var accent: Color {
        guard let hex = row.activeAccent, hex.count == 7, hex.hasPrefix("#"),
              let value = UInt32(hex.dropFirst(), radix: 16) else { return Semantic.secondaryInk }
        return Color(red: Double((value >> 16) & 0xff) / 255, green: Double((value >> 8) & 0xff) / 255, blue: Double(value & 0xff) / 255)
    }
}
