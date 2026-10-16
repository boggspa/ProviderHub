import SwiftUI

/// Commands the phone is allowed to ask the Mac to perform. The Mac owns execution.
public protocol CompanionLink: AnyObject {
    func send(_ command: RemoteCommand)
}

public protocol CompanionTransport: CompanionLink {
    func connect(text: String)
    func disconnect()
    func snapshot()
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
    private let connection: CompanionTransport
    private let preferences: UserDefaults
    private var resumeEnabled = true
    public var selectedChat: RemoteChat? { store.chats.first { $0.id == selectedChatID } }
    public var isConnected: Bool { status == "Connected" }
    public var canSend: Bool {
        isConnected && selectedChat != nil && selectedChat?.busy != true &&
            !draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty &&
            !pendingDrafts.values.contains(where: { $0.0 == selectedChatID && $0.1 == draft })
    }

    public init(preferences: UserDefaults = .standard, transport: CompanionTransport? = nil) {
        self.preferences = preferences
        let native = transport == nil ? RemoteConnection() : nil
        connection = transport ?? native!
        link = connection
        native?.onStatus = { [weak self] status, code in
            DispatchQueue.main.async {
                self?.status = status; self?.confirmCode = code
                if status.hasPrefix("Disconnected") || status.hasPrefix("Command failed") {
                    self?.pendingDrafts.removeAll()
                }
            }
        }
        native?.onMessage = { [weak self] message in
            DispatchQueue.main.async { self?.receiveRemote(message) }
        }
        pairingText = preferences.string(forKey: "remote-bootstrap") ?? ""
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
                if let next, store.selectedID == next { requestControls(next) }
            } else if message["method"] as? String == "session.delta",
                      let event = (message["params"] as? [String: Any])?["event"] as? [String: Any],
                      ["selected", "snapshot"].contains(event["event"] as? String ?? ""),
                      let next = store.selectedID {
                if selectedChatID == nil || !store.chats.contains(where: { $0.id == selectedChatID }) {
                    selectedChatID = next
                }
                if next == selectedChatID {
                requestControls(next)
                }
            }
            if store.needsSnapshot { connection.snapshot() }
            if let selected = selectedChatID, !store.chats.contains(where: { $0.id == selected }),
               let replacement = store.selectedID, store.chats.contains(where: { $0.id == replacement }) {
                selectedChatID = replacement
                requestControls(replacement)
            }
        } catch { status = "Invalid server event" }
    }

    public func connect() {
        do { _ = try RemoteBootstrap.decodePairingText(pairingText) }
        catch { status = error.localizedDescription; return }
        resumeEnabled = true
        preferences.set(pairingText, forKey: "remote-bootstrap")
        connection.connect(text: pairingText)
    }
    public func connect(scannedCode: String) {
        do { _ = try RemoteBootstrap.decodePairingText(scannedCode) }
        catch { status = error.localizedDescription; return }
        pairingText = scannedCode
        connect()
    }
    public func disconnect() {
        resumeEnabled = false
        connection.disconnect()
    }
    /// Resume the pinned Mac endpoint, not an unsubmitted edit in the pairing
    /// field. Keychain identity and Mac allowlist decide trust as before.
    public func resumeSavedConnection() {
        guard resumeEnabled, !isConnected,
              let saved = preferences.string(forKey: "remote-bootstrap"),
              (try? RemoteBootstrap.decodePairingText(saved)) != nil else { return }
        connection.connect(text: saved)
    }
    public func select(_ id: String) {
        selectedChatID = id
        link?.send(RemoteCommand(method: "chat.select", requestId: UUID().uuidString, chat: id))
    }
    private func requestControls(_ id: String) {
        guard controlSurfaceAvailable else { return }
        var command = RemoteCommand(method: "chat.control", requestId: UUID().uuidString, chat: id)
        command.action = "remote_inspect"; command.arguments = [:]
        link?.send(command)
    }

    public func apply(method: String, snapshot: RemoteSnapshot? = nil, delta: RemoteDelta? = nil, requestId: String? = nil, ok: Bool? = nil) {
        store.apply(method: method, snapshot: snapshot, delta: delta, requestId: requestId, ok: ok)
        if selectedChatID == nil { selectedChatID = store.chats.first?.id }
    }

    public func sendDraft() {
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard canSend, let chat = selectedChatID, !text.isEmpty,
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
    public func answerQuestion(_ question: RemoteQuestion, chat: String, answers: [String: [String]], dismiss: Bool) {
        guard isConnected else { return }
        var command = RemoteCommand(method: "chat.answer_question", requestId: UUID().uuidString, chat: chat)
        command.questionId = question.id; command.answers = answers; command.dismiss = dismiss
        link?.send(command)
    }
}

@MainActor
public struct CompanionView: View {
    @ObservedObject var model: CompanionModel
    @State private var scanning = false
    @State private var settings = false
    @State private var scanAfterSettings = false
    @State private var chatList = false
    @State private var inspector = false
    @State private var inspectorTab: RemoteInspectorTab = .agents
    @State private var permissionChange: String?
    @State private var choosingModel = false
    @Environment(\.scenePhase) private var scenePhase
    @State private var viewWidth: CGFloat = 0
    @AppStorage("companionAppearance") private var appearance = "system"
    @AppStorage("companionSurface") private var surfaceStyle = "solid"

    public init(model: CompanionModel) { self.model = model }

    public var body: some View {
        GeometryReader { geometry in
            let wide = geometry.size.width >= 800
            HStack(spacing: 0) {
                if wide && !model.store.chats.isEmpty {
                    sidebar.frame(width: 250)
                    CompanionTheme.hairline.frame(width: 1)
                }
                VStack(spacing: 0) {
                    header(showsRailButton: !wide)
                    CompanionTheme.hairline.frame(height: 1)
                    if let chat = model.selectedChat {
                        if model.store.selectedID == chat.id {
                            CompanionTranscript(entries: model.store.entries, chat: chat, connected: model.isConnected,
                                models: model.models, members: model.members, fallbackRoute: String(model.configuration["choice"].text.split(separator: "|").first ?? "")) { question, answers, dismiss in
                                model.answerQuestion(question, chat: chat.id, answers: answers, dismiss: dismiss)
                            }.id(chat.id)
                        } else {
                            VStack(spacing: 10) { ProgressView(); Text("Opening conversation…").font(.caption).foregroundStyle(CompanionTheme.secondary) }
                                .frame(maxWidth: .infinity, maxHeight: .infinity)
                        }
                        if let approval = model.store.approval { approvalStrip(approval) }
                        composer(chat)
                    } else { welcome }
                }.frame(maxWidth: .infinity, maxHeight: .infinity)
                if !horizontalInspectorIsSheet && inspector, model.selectedChat != nil {
                    CompanionTheme.hairline.frame(width: 1)
                    CompanionInspector(model: model, tab: $inspectorTab).frame(width: min(370, geometry.size.width * 0.32))
                }
            }
            .onChange(of: geometry.size.width, initial: true) { _, width in viewWidth = width }
        }
        .foregroundStyle(CompanionTheme.ink)
        .background {
            if surfaceStyle == "glass" { Rectangle().fill(.ultraThinMaterial).ignoresSafeArea() }
            else { CompanionTheme.surface.ignoresSafeArea() }
        }
        .preferredColorScheme(appearance == "light" ? .light : appearance == "dark" ? .dark : nil)
        .onChange(of: scenePhase, initial: true) { _, phase in
            if phase == .active { model.resumeSavedConnection() }
        }
        .sheet(isPresented: $chatList) {
            NavigationStack { sidebar.navigationTitle("Chats").toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Done") { chatList = false } }
            } }.presentationDetents([.medium, .large])
        }
        .sheet(isPresented: $settings, onDismiss: {
            if scanAfterSettings { scanAfterSettings = false; scanning = true }
        }) { settingsSheet }
        .sheet(isPresented: $choosingModel) {
            CompanionRoutePicker(model: model) { row in model.configure(["choice": row["id"], "effort": .string("")]) }
        }
        .sheet(isPresented: Binding(get: { inspector && horizontalInspectorIsSheet }, set: { inspector = $0 })) {
            NavigationStack {
                CompanionInspector(model: model, tab: $inspectorTab)
                    .navigationTitle("Inspector")
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { inspector = false } } }
            }.presentationDetents([.large])
        }
        .confirmationDialog("Change permissions for this chat?", isPresented: Binding(get: { permissionChange != nil }, set: { if !$0 { permissionChange = nil } })) {
            if let mode = permissionChange {
                Button("Use \(permissionLabel(mode))", role: mode == "yolo" ? .destructive : nil) {
                    model.configure(["approvalMode": .string(mode)]); permissionChange = nil
                }
            }
        } message: {
            Text(permissionChange == "yolo" ? "YOLO allows available tools on your Mac without approval prompts. This also applies to the Team." : "This changes the Mac’s approval policy for this chat and its Team.")
        }
        #if os(iOS)
        .sheet(isPresented: $scanning) {
            PairingScannerView { text in
                scanning = false
                model.connect(scannedCode: text)
            }
        }
        #endif
    }

    private func header(showsRailButton: Bool) -> some View {
        VStack(alignment: .leading, spacing: 2) {
            HStack(spacing: 8) {
                if showsRailButton && !model.store.chats.isEmpty {
                    Button { chatList = true } label: { Image(systemName: "sidebar.left").frame(width: 40, height: 40) }
                        .accessibilityLabel("Show chats")
                }
                Image(systemName: "square.stack.3d.up.fill").font(.caption).foregroundStyle(CompanionTheme.brand)
                Text("Provider Hub").font(.subheadline.weight(.semibold))
                Spacer(minLength: 4)
                Circle().fill(model.isConnected ? Color.green : CompanionTheme.secondary).frame(width: 6, height: 6)
                Text(model.isConnected ? "Mac connected" : "Offline").font(.caption2).foregroundStyle(CompanionTheme.secondary)
                if model.selectedChat != nil {
                    Button { inspector.toggle() } label: { Image(systemName: "sidebar.right").frame(width: 40, height: 40) }
                        .accessibilityLabel("Show inspector")
                        .keyboardShortcut("i", modifiers: [.command, .option])
                }
                Button { settings = true } label: { Image(systemName: "gearshape").frame(width: 40, height: 40) }
                    .accessibilityLabel("Connection and appearance settings")
            }.buttonStyle(.plain)
            if let chat = model.selectedChat {
                VStack(alignment: .leading, spacing: 5) {
                    Text(chat.title).font(.headline).lineLimit(1)
                    HStack(spacing: 6) {
                        CompanionProviderMark(presentation: CompanionBranding.presentation(route: chat.route ?? String(model.configuration["choice"].text.split(separator: "|").first ?? ""), label: chat.displayModel, models: model.models), size: 15)
                        if model.team["enabled"].flag {
                            Button { inspectorTab = .agents; inspector = true } label: {
                                Text("Team · \(model.members.count) models").font(.caption.weight(.medium))
                            }.buttonStyle(.plain)
                        } else {
                            Button { choosingModel = true } label: {
                                HStack(spacing: 5) { Text(chat.displayModel).font(.caption.weight(.medium)); Image(systemName: "chevron.down").font(.system(size: 9)) }
                            }.buttonStyle(.plain)
                                .disabled(!model.isConnected || model.configuration["busy"].flag || !model.controlSurfaceAvailable)
                        }
                        Menu {
                            ForEach(["manual", "accept_edits", "yolo"], id: \.self) { mode in
                                Button(permissionLabel(mode)) { permissionChange = mode }
                            }
                        } label: {
                            Label(permissionLabel(model.configuration["approvalMode"].text.isEmpty ? chat.approvalMode ?? "manual" : model.configuration["approvalMode"].text), systemImage: "lock.shield")
                                .font(.caption)
                        }.disabled(!model.isConnected || model.configuration["busy"].flag || !model.controlSurfaceAvailable)
                        if !model.team["enabled"].flag {
                            Menu {
                                Button("Default") { model.configure(["effort": .string("")]) }
                                ForEach(model.models.first(where: { $0["id"] == model.configuration["choice"] })?["efforts"].rows ?? [], id: \.self) { effort in
                                    Button(effort.text.capitalized) { model.configure(["effort": effort]) }
                                }
                            } label: {
                                Text(model.configuration["effort"].text.isEmpty ? "Reasoning" : model.configuration["effort"].text.capitalized).font(.caption)
                            }.disabled(!model.isConnected || model.configuration["busy"].flag || !model.controlSurfaceAvailable)
                        }
                        Text("· On your Mac").font(.caption).foregroundStyle(CompanionTheme.secondary)
                    }.lineLimit(1)
                    if model.team["enabled"].flag {
                        ScrollView(.horizontal, showsIndicators: false) {
                            HStack(spacing: 8) {
                                ForEach(model.members, id: \.self) { member in
                                    Button { inspectorTab = .agents; inspector = true } label: {
                                        let brand = CompanionBranding.presentation(route: member["route"].text.isEmpty ? String(member["choice"].text.split(separator: "|").first ?? "") : member["route"].text, label: member["label"].text, models: model.models)
                                        ChatSharedMemberChip(name: member["name"].text, model: member["label"].text,
                                            used: member["usage"].optionalInt, limit: member["context"].optionalInt,
                                            active: member["status"].text == "working", accent: brand.accent,
                                            ink: CompanionTheme.ink, secondary: CompanionTheme.secondary) {
                                                CompanionProviderMark(presentation: brand, size: 13)
                                            }.padding(.vertical, 6)
                                    }.buttonStyle(.plain)
                                }
                            }
                        }
                    }
                }.padding(.bottom, 12)
            }
        }.padding(.horizontal, CompanionTheme.margin)
    }

    private var horizontalInspectorIsSheet: Bool {
        viewWidth < 1000
    }
    private func permissionLabel(_ mode: String) -> String {
        switch mode { case "accept_edits": return "Accept Edits"; case "yolo": return "YOLO"; default: return "Manual" }
    }

    private var sidebar: some View {
        VStack(alignment: .leading, spacing: 0) {
            Text("Recents").font(.caption.weight(.semibold)).foregroundStyle(CompanionTheme.secondary)
                .padding(.horizontal, 18).padding(.top, 22).padding(.bottom, 12)
            ScrollView {
                LazyVStack(spacing: 3) {
                    ForEach(model.store.chats) { chat in
                        Button {
                            model.select(chat.id); chatList = false
                        } label: {
                            HStack(spacing: 10) {
                                Image(systemName: "bubble.left").font(.caption).foregroundStyle(chat.providerAccent)
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(chat.title).font(.subheadline.weight(.medium)).lineLimit(1)
                                    Text(chat.displayModel).font(.caption2).foregroundStyle(CompanionTheme.secondary).lineLimit(1)
                                }
                                Spacer(minLength: 0)
                                if chat.pendingApproval { Image(systemName: "hand.raised").font(.caption).foregroundStyle(chat.providerAccent) }
                                else if chat.busy { ProgressView().controlSize(.mini).tint(chat.providerAccent) }
                            }.padding(.horizontal, 12).padding(.vertical, 11)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(chat.id == model.selectedChatID ? CompanionTheme.selection : .clear, in: RoundedRectangle(cornerRadius: 8))
                                .overlay(alignment: .leading) {
                                    if chat.id == model.selectedChatID { RoundedRectangle(cornerRadius: 1).fill(chat.providerAccent).frame(width: 2).padding(.vertical, 9) }
                                }
                        }.buttonStyle(.plain)
                    }
                }.padding(.horizontal, 8)
            }
            HStack(spacing: 7) { Image(systemName: "desktopcomputer"); Text(model.isConnected ? "Connected to your Mac" : "Saved connection") }
                .font(.caption).foregroundStyle(CompanionTheme.secondary).padding(18)
        }.background(CompanionTheme.surface).foregroundStyle(CompanionTheme.ink)
    }

    private var welcome: some View {
        ScrollView {
            VStack(spacing: 20) {
                Image(systemName: "desktopcomputer.and.iphone").font(.system(size: 38, weight: .light))
                    .foregroundStyle(CompanionTheme.secondary).padding(.top, 60)
                VStack(spacing: 8) {
                    Text(model.isConnected ? "Your chats, here" : "Your Mac, within reach").font(.title2.weight(.semibold))
                    Text(model.isConnected ? "Start a chat in Provider Hub on your Mac. It will appear here." : "Continue a conversation, check on work and answer approvals from your phone.")
                        .font(.callout).foregroundStyle(CompanionTheme.secondary).multilineTextAlignment(.center)
                }
                if !model.isConnected { pairingControls }
                if !model.isConnected && !["Not paired", "Disconnected"].contains(model.status) {
                    Text(model.status).font(.caption).foregroundStyle(CompanionTheme.secondary).multilineTextAlignment(.center)
                }
            }.padding(22).frame(maxWidth: 440).frame(maxWidth: .infinity)
        }
    }

    private var pairingControls: some View {
        VStack(alignment: .leading, spacing: 14) {
            Text("Away from home? Connect Tailscale on your iPhone and Mac to the same tailnet, then pair using the Mac’s Tailscale IP. Works over cellular and other Wi-Fi networks. Keep Provider Hub running on your Mac.")
                .font(.caption).foregroundStyle(CompanionTheme.secondary)
            #if os(iOS)
            Button {
                if settings { scanAfterSettings = true; settings = false }
                else { scanning = true }
            } label: {
                Label("Scan your Mac’s QR code", systemImage: "qrcode.viewfinder")
                    .font(.subheadline.weight(.semibold)).frame(maxWidth: .infinity).padding(.vertical, 8)
            }.buttonStyle(.borderedProminent).tint(CompanionTheme.ink).foregroundStyle(CompanionTheme.surface)
            #endif
            DisclosureGroup("Use pairing text") {
                VStack(spacing: 10) {
                    TextField("Paste from Devices on your Mac", text: $model.pairingText, axis: .vertical)
                        .lineLimit(2...4).font(.system(.caption, design: .monospaced)).textFieldStyle(.plain)
                        .padding(12).background(CompanionTheme.raised, in: RoundedRectangle(cornerRadius: 8))
                    Button("Connect", action: model.connect).buttonStyle(.bordered).frame(maxWidth: .infinity, alignment: .trailing)
                }.padding(.top, 10)
            }.font(.subheadline).tint(CompanionTheme.secondary)
            if let code = model.confirmCode, !model.isConnected {
                VStack(spacing: 6) {
                    Text(code).font(.system(.title, design: .monospaced).weight(.semibold))
                    Text("Match this code and approve pairing on your Mac.").font(.caption).foregroundStyle(CompanionTheme.secondary)
                }.frame(maxWidth: .infinity).padding(.top, 4)
            }
        }.padding(16).companionCard(cornerRadius: CompanionTheme.panelRadius)
    }

    private func approvalStrip(_ approval: RemoteApproval) -> some View {
        VStack(alignment: .leading, spacing: 10) {
            HStack(spacing: 7) {
                Image(systemName: "hand.raised.fill").foregroundStyle(CompanionTheme.manual)
                Text("Approval needed").font(.subheadline.weight(.semibold))
                Spacer()
                if approval.chat != model.selectedChatID { Text(model.store.chats.first { $0.id == approval.chat }?.title ?? "Another chat").font(.caption).lineLimit(1) }
            }
            ScrollView { Text(approval.summary).font(.system(.caption, design: .monospaced)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading) }
                .frame(maxHeight: 90)
            if !approval.detail.isEmpty {
                DisclosureGroup("Action details") { ScrollView { Text(approval.detail).font(.system(.caption2, design: .monospaced)).textSelection(.enabled) }.frame(maxHeight: 100) }
                    .font(.caption).tint(CompanionTheme.secondary)
            }
            HStack { Spacer(); Button("Deny") { model.decide(false) }.buttonStyle(.bordered)
                Button("Allow once") { model.decide(true) }.buttonStyle(.borderedProminent).tint(CompanionTheme.ink).foregroundStyle(CompanionTheme.surface) }
                .disabled(!model.isConnected)
        }.padding(14).companionCard(cornerRadius: CompanionTheme.panelRadius, accent: CompanionTheme.manual)
            .padding(.horizontal, 14).padding(.bottom, 8)
    }

    private func composer(_ chat: RemoteChat) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !model.isConnected || model.store.needsSnapshot {
                Text(model.store.needsSnapshot ? "Refreshing after a missed update…" : model.status)
                    .font(.caption).foregroundStyle(CompanionTheme.secondary).lineLimit(2)
            } else if let notice = model.store.notices.last {
                Text(notice).font(.caption).foregroundStyle(CompanionTheme.secondary).lineLimit(2)
            }
            HStack(alignment: .bottom, spacing: 10) {
                TextField("Message…", text: $model.draft, axis: .vertical)
                    .textFieldStyle(.plain).lineLimit(1...6).font(.body).padding(.vertical, 10)
                    .accessibilityLabel("Message to \(chat.title)")
                Button { if chat.busy { model.stop() } else { model.sendDraft() } } label: {
                    Image(systemName: chat.busy ? "stop.fill" : "arrow.up")
                        .font(.system(size: 16, weight: .bold)).foregroundStyle(CompanionTheme.surface)
                        .frame(width: 36, height: 36).background(CompanionTheme.ink, in: Circle()).padding(.vertical, 4)
                }.buttonStyle(.plain).disabled(!model.isConnected || !chat.busy && !model.canSend)
                    .opacity(!chat.busy && !model.canSend ? 0.4 : 1)
                    .accessibilityLabel(chat.busy ? "Stop this chat" : "Send message")
            }.padding(.horizontal, 14).padding(.vertical, 4)
                .companionCard(cornerRadius: 20, accent: chat.approvalMode == "yolo" ? .red : chat.providerAccent)
            HStack(spacing: 6) {
                Circle().fill(chat.approvalColor).frame(width: 5, height: 5)
                Text(chat.approvalLabel).foregroundStyle(chat.approvalColor)
                Text(chat.busy ? "Working…" : "Ready").foregroundStyle(CompanionTheme.secondary)
                Spacer()
                Text("Runs on your Mac").foregroundStyle(CompanionTheme.secondary)
            }.font(.caption2).padding(.horizontal, 5)
        }.padding(.horizontal, 14).padding(.top, 8).padding(.bottom, 10)
    }

    private var settingsSheet: some View {
        NavigationStack {
            Form {
                Section("Your Mac") {
                    LabeledContent("Connection", value: model.isConnected ? "Connected" : "Offline")
                    if model.isConnected { Button("Disconnect", action: model.disconnect) }
                    else { pairingControls }
                    Text("Provider keys and execution stay on your Mac. Remote actions use the chat’s existing approval mode.").font(.caption).foregroundStyle(.secondary)
                }
                Section("Appearance") {
                    Picker("Theme", selection: $appearance) { Text("System").tag("system"); Text("Light").tag("light"); Text("Dark").tag("dark") }
                    Picker("Surface", selection: $surfaceStyle) { Text("Solid").tag("solid"); Text("Glass").tag("glass") }
                }
            }.navigationTitle("Settings")
                .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { settings = false } } }
        }.presentationDetents([.medium, .large])
    }
}
