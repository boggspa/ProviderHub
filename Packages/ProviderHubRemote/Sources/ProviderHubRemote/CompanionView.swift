import SwiftUI

/// Commands the phone is allowed to ask the Mac to perform. The Mac owns execution.
public protocol CompanionLink: AnyObject {
    func send(_ command: RemoteCommand)
}

public final class CompanionModel: ObservableObject {
    @Published public var store = CompanionStore()
    @Published public var draft = ""
    @Published public var confirmCode: String?
    @Published public var status = "Not paired"
    @Published public var selectedChatID: String? {
        didSet {
            if let oldValue { drafts[oldValue] = draft }
            draft = selectedChatID.flatMap { drafts[$0] } ?? ""
        }
    }
    private var drafts: [String: String] = [:]
    private var pendingDrafts: [String: (String, String)] = [:]
    public weak var link: CompanionLink?
    @Published public var pairingText = ""
    private let connection = RemoteConnection()

    public init() {
        link = connection
        connection.onStatus = { [weak self] status, code in
            DispatchQueue.main.async {
                self?.status = status; self?.confirmCode = code
                if status.hasPrefix("Disconnected") || status.hasPrefix("Command failed") {
                    self?.pendingDrafts.removeAll()
                }
            }
        }
        connection.onMessage = { [weak self] message in
            DispatchQueue.main.async { self?.receiveRemote(message) }
        }
        pairingText = UserDefaults.standard.string(forKey: "remote-bootstrap") ?? ""
    }

    func receiveRemote(_ message: [String: Any]) {
        do {
            try store.receive(message)
            if message["method"] as? String == "session.ack",
               let params = message["params"] as? [String: Any],
               let id = params["requestId"] as? String,
               let (chat, text) = pendingDrafts.removeValue(forKey: id),
               params["ok"] as? Bool == true {
                if drafts[chat] == text { drafts[chat] = "" }
                if selectedChatID == chat && draft == text { draft = "" }
            }
            if message["method"] as? String == "session.snapshot" {
                let saved = selectedChatID.flatMap { id in store.chats.contains(where: { $0.id == id }) ? id : nil }
                let next = saved ?? store.selectedID ?? store.chats.first?.id
                selectedChatID = next
                if let next, store.selectedID != next { select(next) }
            }
            if store.needsSnapshot { connection.snapshot() }
        } catch { status = "Invalid server event" }
    }

    public func connect() {
        do { _ = try RemoteBootstrap.decodePairingText(pairingText) }
        catch { status = error.localizedDescription; return }
        UserDefaults.standard.set(pairingText, forKey: "remote-bootstrap")
        connection.connect(text: pairingText)
    }
    public func connect(scannedCode: String) {
        do { _ = try RemoteBootstrap.decodePairingText(scannedCode) }
        catch { status = error.localizedDescription; return }
        pairingText = scannedCode
        connect()
    }
    public func disconnect() { connection.disconnect() }
    public func select(_ id: String) {
        selectedChatID = id
        link?.send(RemoteCommand(method: "chat.select", requestId: UUID().uuidString, chat: id))
    }

    public func apply(method: String, snapshot: RemoteSnapshot? = nil, delta: RemoteDelta? = nil, requestId: String? = nil, ok: Bool? = nil) {
        store.apply(method: method, snapshot: snapshot, delta: delta, requestId: requestId, ok: ok)
        if selectedChatID == nil { selectedChatID = store.chats.first?.id }
    }

    public func sendDraft() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard status == "Connected", let chat = selectedChatID, !text.isEmpty,
              !pendingDrafts.values.contains(where: { $0.0 == chat && $0.1 == draft }) else { return }
        let id = UUID().uuidString
        pendingDrafts[id] = (chat, draft)
        link?.send(RemoteCommand(method: "chat.send", requestId: id, chat: chat, text: text))
    }

    public func stop() {
        guard let chat = selectedChatID else { return }
        link?.send(RemoteCommand(method: "chat.stop", requestId: UUID().uuidString, chat: chat))
    }

    public func decide(_ allow: Bool) {
        guard let approval = store.approval, let chat = approval.chat else { return }
        link?.send(RemoteCommand(method: "chat.approve", requestId: UUID().uuidString, chat: chat, approvalId: approval.id, allow: allow))
    }
}

public struct CompanionView: View {
    @ObservedObject var model: CompanionModel
    @State private var scanning = false

    public init(model: CompanionModel) { self.model = model }

    public var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text("Provider Hub").font(.title2.bold())
            Text(model.status).font(.subheadline).foregroundStyle(.secondary)
            TextField("Paste pairing text from your Mac", text: $model.pairingText)
                .textFieldStyle(.roundedBorder)
            HStack {
                Button("Connect", action: model.connect)
                Button("Disconnect", action: model.disconnect)
                #if os(iOS)
                Spacer()
                Button { scanning = true } label: { Label("Scan QR code", systemImage: "qrcode.viewfinder") }
                #endif
            }
            Text("Provider keys stay on the Mac. This phone only sends, stops, and answers the approval already pending there.")
                .font(.footnote)
                .foregroundStyle(.secondary)
            if let code = model.confirmCode {
                Text("Pairing code \(code)").font(.title3.monospaced())
            }
            if model.store.chats.isEmpty {
                Text("No chats yet").foregroundStyle(.secondary)
            } else {
                Picker("Chat", selection: Binding(get: { model.selectedChatID }, set: { if let id = $0 { model.select(id) } })) {
                    ForEach(model.store.chats) { chat in
                        Text(chat.title + (chat.busy ? " · Running" : "")).tag(Optional(chat.id))
                    }
                }.pickerStyle(.menu)
            }
            ScrollView { Text(model.store.transcript).frame(maxWidth: .infinity, alignment: .leading).textSelection(.enabled) }
            ForEach(Array(model.store.notices.suffix(3).enumerated()), id: \.offset) { _, notice in
                Text(notice).font(.caption)
            }
            if let approval = model.store.approval {
                VStack(alignment: .leading, spacing: 8) {
                    Text("Approval").font(.headline)
                    Text(approval.summary)
                    if !approval.detail.isEmpty { Text(approval.detail).font(.caption) }
                    HStack {
                        Button("Allow") { model.decide(true) }
                        Button("Deny") { model.decide(false) }
                    }
                }
                .padding(8)
                .background(.quaternary, in: RoundedRectangle(cornerRadius: 8))
            }
            TextField("Message", text: $model.draft)
                .textFieldStyle(.roundedBorder)
            HStack {
                Button("Send", action: model.sendDraft)
                Button("Stop", action: model.stop)
            }
            if model.store.needsSnapshot {
                Text("Missed events. Waiting for a fresh snapshot.").font(.caption)
            }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
        #if os(iOS)
        .sheet(isPresented: $scanning) {
            PairingScannerView { text in
                scanning = false
                model.connect(scannedCode: text)
            }
        }
        #endif
    }
}
