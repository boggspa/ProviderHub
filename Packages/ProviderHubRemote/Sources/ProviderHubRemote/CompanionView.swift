import SwiftUI
import UniformTypeIdentifiers
#if os(iOS)
import PhotosUI
#endif

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
    var pendingControls: [String: CheckedContinuation<RemoteValue, Error>] = [:]
    @Published public var attachmentDrafts: [String: [RemoteDraftAttachment]] = [:]
    @Published public var attachmentSending = false
    @Published public var uploadProgress = ""
    @Published public var composeNotice = ""
    @Published public var historyLoading = false
    @Published public var downloadedFiles: [String: URL] = [:]
    @Published public var fullModelCatalogue: [RemoteValue] = []
    @Published public var attachmentThumbnails: [String: Data] = [:]
    @Published var activityJumpID: String?
    @Published var activityJumpToken = 0
    /// When the selected chat's open run began, for the footer's turn clock.
    @Published public private(set) var turnStartedAt: Date?
    private var turnChatID: String?
    var fileDownloads = Set<String>()
    public weak var link: CompanionLink?
    @Published public var pairingText = ""
    private let connection: CompanionTransport
    private let preferences: UserDefaults
    private var resumeEnabled = true
    public var selectedChat: RemoteChat? { store.chats.first { $0.id == selectedChatID } }
    public var isConnected: Bool { status == "Connected" }
    public var canSend: Bool {
        isConnected && selectedChat != nil && !runIsOpen &&
            (!draft.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || !(attachmentDrafts[selectedChatID ?? ""] ?? []).isEmpty) && !attachmentSending &&
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
                    if let self {
                        let pending = self.pendingControls; self.pendingControls.removeAll()
                        for continuation in pending.values { continuation.resume(throwing: RemoteControlFailure.rejected("Disconnected; reconnect and inspect before retrying. Your draft is retained.")) }
                    }
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
            if message["method"] as? String == "session.ack", let params = message["params"] as? [String: Any],
               let id = params["requestId"] as? String, let continuation = pendingControls.removeValue(forKey: id) {
                if params["ok"] as? Bool == true {
                    do {
                        let data = try JSONSerialization.data(withJSONObject: params["result"] ?? [:], options: [.fragmentsAllowed])
                        continuation.resume(returning: try JSONDecoder().decode(RemoteValue.self, from: data))
                    } catch { continuation.resume(throwing: error) }
                } else { continuation.resume(throwing: RemoteControlFailure.rejected(params["error"] as? String ?? "The Mac rejected this action")) }
            }
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
                // A full snapshot restores presentation, not the server's
                // remote subscription. Rebind even when both IDs match so
                // controls and live activity are admitted after a reconnect.
                if let next { select(next) }
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
            let delta = (message["params"] as? [String: Any])?["event"] as? [String: Any]
            updateTurnClock(resumed: message["method"] as? String == "session.snapshot" ||
                ["selected", "snapshot"].contains(delta?["event"] as? String ?? ""))
        } catch { status = "Invalid server event" }
    }

    /// The Mac's turn clock starts when its run opens and is not reset by a
    /// steer. A run the phone sees open starts now; one already under way when
    /// the phone arrives (a reconnect, another chat) counts from the message
    /// that opened it, which is the closest the phone can know.
    private func updateTurnClock(resumed: Bool) {
        guard runIsOpen, let chat = selectedChatID, store.selectedID == chat else {
            if turnStartedAt != nil { turnStartedAt = nil }
            turnChatID = nil; return
        }
        guard turnStartedAt == nil || turnChatID != chat else { return }
        turnChatID = chat
        let opened = resumed ? store.visibleEntries.last(where: { $0.role == "user" })?.time.map(Date.init(timeIntervalSince1970:)) : nil
        turnStartedAt = min(opened ?? Date(), Date())
    }

    /// "Connecting" or "Offline" while the Mac is out of reach; nil when it is
    /// connected or was never paired. A live connection needs no label.
    public var connectionPhase: String? {
        if isConnected || status == "Not paired" { return nil }
        return status.contains("onnecting") || status.hasPrefix("Compare code") ? "Connecting" : "Offline"
    }

    /// The selected chat's approval mode, read once: the Mac's configuration
    /// first, the recent-list summary (which may lag it) as a fallback.
    public var approvalMode: String {
        let configured = selectionBound ? configuration["approvalMode"].text : ""
        return configured.isEmpty ? selectedChat?.approvalMode ?? "manual" : configured
    }
    /// The run-time settings (model, effort, approval mode) can change only
    /// between turns, as on the Mac.
    public var configurable: Bool {
        isConnected && controlSurfaceAvailable && selectionBound && !runIsOpen && !configuration["busy"].flag
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
        if id != selectedChatID { store.clearNotices() }
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
        if !(attachmentDrafts[selectedChatID ?? ""] ?? []).isEmpty {
            guard canSend else { return }
            Task { @MainActor in await sendWithAttachments() }
            return
        }
        let text = draft.trimmingCharacters(in: .whitespacesAndNewlines)
        guard canSend, let chat = selectedChatID, !text.isEmpty,
              !pendingDrafts.values.contains(where: { $0.0 == chat && $0.1 == draft }) else { return }
        let id = UUID().uuidString
        pendingDrafts[id] = (chat, draft)
        store.clearNotices(); composeNotice = ""
        var command = RemoteCommand(method: "chat.send", requestId: id, chat: chat, text: text)
        let chips = mentionChips(in: text)
        if !chips.isEmpty { command.mentions = chips }
        link?.send(command)
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
    @State private var showingPermissionConfirmation = false
    @State private var choosingModel = false
    @State private var creatingChat = false
    @State private var importingFiles = false
    @StateObject private var mentionMenu = CompanionMentionMenu()
    #if os(iOS)
    @State private var photos: [PhotosPickerItem] = []
    @State private var showingPhotos = false
    #endif
    @Environment(\.scenePhase) private var scenePhase
    @State private var viewWidth: CGFloat = 0
    @AppStorage("companionAppearance") private var appearance = "system"
    @AppStorage("companionSurface") private var surfaceStyle = "solid"
    @AppStorage("companionRailVisible") private var railVisible = true

    public init(model: CompanionModel) { self.model = model }

    public var body: some View {
        GeometryReader { geometry in
            let wide = geometry.size.width >= 800
            HStack(spacing: 0) {
                if wide && railVisible && !model.store.chats.isEmpty {
                    sidebar.frame(width: 250)
                    CompanionTheme.hairline.frame(width: 1)
                }
                VStack(spacing: 0) {
                    header(wide: wide)
                    if model.isConnected && !model.extendedControlsAvailable {
                        Text("Update the Mac host to enable older messages, attachments, new chats and Blackboard.")
                            .font(.caption).foregroundStyle(.orange).padding(.horizontal, 18).padding(.bottom, 8)
                    }
                    CompanionTheme.hairline.frame(height: 1)
                    // One sheet of paper from the header rule to the bottom
                    // edge, as on the Mac: the live strip and the composer sit
                    // on the transcript rather than on a band under a hard edge.
                    VStack(spacing: 0) {
                        if let chat = model.selectedChat {
                            if model.store.selectedID == chat.id {
                                CompanionTranscript(entries: model.store.visibleEntries, chat: chat, connected: model.isConnected,
                                    models: model.models, members: model.members, fallbackRoute: String(model.configuration["choice"].text.split(separator: "|").first ?? ""),
                                    hasOlder: model.store.hasOlder && model.extendedControlsAvailable, loadingOlder: model.historyLoading,
                                    loadOlder: { Task { @MainActor in await model.loadOlder() } }, turnSummaries: model.store.turnSummaries,
                                    runIsOpen: model.runIsOpen, activities: model.liveActivities, jumpEntryID: model.activityJumpID,
                                    jumpToken: model.activityJumpToken, workspace: model.configuration["workspace"].text,
                                    teamEnabled: model.team["enabled"].flag) { question, answers, dismiss in
                                    model.answerQuestion(question, chat: chat.id, answers: answers, dismiss: dismiss)
                                }
                                .id(chat.id)
                            } else {
                                VStack(spacing: 10) { ProgressView(); Text("Opening conversation…").font(.caption).foregroundStyle(CompanionTheme.secondary) }
                                    .frame(maxWidth: .infinity, maxHeight: .infinity)
                            }
                            CompanionActivityStrip(model: model)
                            if let approval = model.store.approval { approvalStrip(approval) }
                            composer(chat)
                        } else { welcome }
                    }
                    .background(CompanionTheme.pane(.transcript, surfaceStyle: surfaceStyle).ignoresSafeArea(edges: .bottom))
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
        .task(id: "\(model.isConnected)-\(model.store.controls["extensionVersion"]?.int ?? 0)") {
            if model.isConnected && model.controlSurfaceAvailable { await model.refreshRemoteOptions() }
        }
        .sheet(isPresented: $creatingChat) { CompanionNewChat(model: model) }
        .fileImporter(isPresented: $importingFiles, allowedContentTypes: [.image, .pdf, .text, .sourceCode, .json, .data], allowsMultipleSelection: true) { result in
            do {
                for url in try result.get() {
                    let access = url.startAccessingSecurityScopedResource()
                    defer { if access { url.stopAccessingSecurityScopedResource() } }
                    let values = try url.resourceValues(forKeys: [.fileSizeKey])
                    guard (values.fileSize ?? 0) <= 8 * 1024 * 1024 else { throw RemoteControlFailure.rejected("Choose files under 8 MiB") }
                    try model.addAttachment(name: url.lastPathComponent, data: Data(contentsOf: url))
                }
            } catch { model.composeNotice = error.localizedDescription }
        }
        #if os(iOS)
        .photosPicker(isPresented: $showingPhotos, selection: $photos, maxSelectionCount: 8, matching: .images)
        .onChange(of: photos) { _, selected in
            Task { @MainActor in
                for item in selected {
                    do {
                        guard let data = try await item.loadTransferable(type: Data.self) else { continue }
                        guard let image = UIImage(data: data), let jpeg = image.jpegData(compressionQuality: 0.88) else { throw RemoteControlFailure.rejected("This photo could not be decoded") }
                        try model.addAttachment(name: "Photo-\(UUID().uuidString.prefix(8)).jpg", data: jpeg)
                    } catch { model.composeNotice = error.localizedDescription }
                }
                photos = []
            }
        }
        #endif
        .sheet(isPresented: $chatList) {
            NavigationStack { sidebar.navigationTitle("Chats").toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Done") { chatList = false } }
            } }.presentationDetents([.medium, .large])
        }
        .sheet(isPresented: $settings, onDismiss: {
            if scanAfterSettings { scanAfterSettings = false; scanning = true }
        }) { settingsSheet }
        .sheet(isPresented: Binding(get: { inspector && horizontalInspectorIsSheet }, set: { inspector = $0 })) {
            NavigationStack {
                CompanionInspector(model: model, tab: $inspectorTab)
                    .navigationTitle("Inspector")
                    .toolbar { ToolbarItem(placement: .cancellationAction) { Button("Done") { inspector = false } } }
            }.presentationDetents([.large])
        }
        .confirmationDialog("Run tools without approval prompts?", isPresented: $showingPermissionConfirmation, titleVisibility: .visible) {
            Button("Use YOLO", role: .destructive) { model.configure(["approvalMode": .string("yolo")]) }
        } message: {
            Text("YOLO lets tools edit files and run commands on your Mac without asking first. This also applies to the Team.")
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

    /// One row of controls with the chat's title between them, then the
    /// chat's model (or Team) beneath. The connection only shows when it is
    /// missing: an open, connected app needs no label saying so.
    private func header(wide: Bool) -> some View {
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 0) {
                if !model.store.chats.isEmpty {
                    Button { if wide { railVisible.toggle() } else { chatList = true } } label: { Image(systemName: "sidebar.left").frame(width: 40, height: 40) }
                        .accessibilityLabel(wide && railVisible ? "Hide chats" : "Show chats")
                        .keyboardShortcut("s", modifiers: [.command, .control])
                }
                if model.isConnected {
                    Button { creatingChat = true } label: { Image(systemName: "square.and.pencil").frame(width: 40, height: 40) }
                        .accessibilityLabel("New chat").keyboardShortcut("n", modifiers: .command)
                }
                Text(model.selectedChat?.title ?? "")
                    .font(.headline).lineLimit(1).truncationMode(.tail)
                    .padding(.horizontal, 6).frame(maxWidth: .infinity, alignment: .leading)
                    .accessibilityAddTraits(.isHeader)
                if let phase = model.connectionPhase {
                    HStack(spacing: 5) {
                        if phase == "Connecting" { ProgressView().controlSize(.mini) }
                        else { Circle().fill(CompanionTheme.secondary).frame(width: 6, height: 6) }
                        Text(phase).font(.caption2)
                    }.foregroundStyle(CompanionTheme.secondary).padding(.horizontal, 6).fixedSize()
                }
                if model.selectedChat != nil {
                    Button { inspector.toggle() } label: { Image(systemName: "sidebar.right").frame(width: 40, height: 40) }
                        .accessibilityLabel(inspector ? "Hide inspector" : "Show inspector")
                        .keyboardShortcut("i", modifiers: [.command, .control])
                }
                Button { settings = true } label: { Image(systemName: "gearshape").frame(width: 40, height: 40) }
                    .accessibilityLabel("Connection and appearance settings")
            }.buttonStyle(.plain)
            if let chat = model.selectedChat {
                chatControls(chat).padding(.horizontal, 8).padding(.bottom, 10)
            }
        }.padding(.horizontal, 10)
    }

    /// The Mac header's second half: the Team and its members while a Team
    /// runs the chat, otherwise the model (with its reasoning effort) and the
    /// working folder. Effort is chosen in the model picker's sidecar.
    @ViewBuilder private func chatControls(_ chat: RemoteChat) -> some View {
        if model.team["enabled"].flag {
            ScrollView(.horizontal, showsIndicators: false) {
                HStack(spacing: 10) {
                    Button { inspectorTab = .agents; inspector = true } label: {
                        Label("Team · \(model.members.count)", systemImage: "person.3").font(.caption.weight(.medium))
                    }
                    ForEach(model.members, id: \.self) { member in
                        Button { inspectorTab = .agents; inspector = true } label: {
                            let brand = CompanionBranding.presentation(route: member["route"].text.isEmpty ? String(member["choice"].text.split(separator: "|").first ?? "") : member["route"].text, label: member["label"].text, models: model.models)
                            ChatSharedMemberChip(name: member["name"].text, model: member["label"].text,
                                used: member["usage"].optionalInt, limit: member["context"].optionalInt,
                                active: member["status"].text == "working", accent: brand.accent,
                                ink: CompanionTheme.ink, secondary: CompanionTheme.secondary) {
                                    CompanionProviderMark(presentation: brand, size: 13)
                                }.padding(.vertical, 4)
                        }
                    }
                }
            }.buttonStyle(.plain)
        } else {
            HStack(spacing: 10) {
                Button { choosingModel = true } label: {
                    HStack(spacing: 5) {
                        CompanionProviderMark(presentation: CompanionBranding.presentation(route: chat.route ?? String(model.configuration["choice"].text.split(separator: "|").first ?? ""), label: chat.displayModel, models: model.models), size: 15)
                        Text(chat.displayModel).font(.caption.weight(.medium))
                        let effort = model.configuration["effort"].text
                        if !effort.isEmpty { Text("· " + ChatSharedEffort.title(effort)).font(.caption).foregroundStyle(CompanionTheme.secondary) }
                        Image(systemName: "chevron.down").font(.system(size: 9))
                    }
                }.buttonStyle(.plain)
                    .sheet(isPresented: $choosingModel) {
                        CompanionRoutePicker(model: model) { row, effort in model.configure(["choice": row["id"], "effort": .string(effort)]) }
                    }
                    .disabled(!model.configurable)
                    .accessibilityLabel("Model")
                let folder = model.configuration["workspace"].text
                if !folder.isEmpty {
                    Label((folder as NSString).lastPathComponent, systemImage: "folder")
                        .font(.caption).foregroundStyle(CompanionTheme.secondary)
                        .accessibilityLabel("Working folder " + (folder as NSString).lastPathComponent)
                }
            }.lineLimit(1)
        }
    }

    private var horizontalInspectorIsSheet: Bool {
        viewWidth < 1000
    }
    private func choosePermission(_ mode: String) {
        if mode == "yolo" { showingPermissionConfirmation = true }
        else { model.configure(["approvalMode": .string(mode)]) }
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
                                CompanionProviderMark(presentation: CompanionBranding.presentation(route: chat.route ?? "", label: chat.displayModel, models: model.models), size: 14)
                                VStack(alignment: .leading, spacing: 4) {
                                    Text(chat.title).font(.subheadline.weight(.medium)).lineLimit(1)
                                    Text(chat.displayModel).font(.caption2).foregroundStyle(CompanionTheme.secondary).lineLimit(1)
                                }
                                Spacer(minLength: 0)
                                if chat.pendingApproval { Image(systemName: "hand.raised.fill").font(.caption).foregroundStyle(CompanionTheme.secondary) }
                                else if chat.active ?? chat.busy { ProgressView().controlSize(.mini).tint(chat.providerAccent) }
                            }.padding(.horizontal, 12).padding(.vertical, 11)
                                .frame(maxWidth: .infinity, alignment: .leading)
                                .background(chat.id == model.selectedChatID ? CompanionTheme.selection : .clear, in: RoundedRectangle(cornerRadius: 8))
                                .overlay(alignment: .leading) {
                                    if chat.id == model.selectedChatID { RoundedRectangle(cornerRadius: 1).fill(chat.providerAccent).frame(width: 2).padding(.vertical, 9) }
                                }
                        }.buttonStyle(.plain)
                    }
                }.padding(.horizontal, 8).padding(.bottom, 12)
            }
        }.background(CompanionTheme.pane(.sidebar, surfaceStyle: surfaceStyle)).foregroundStyle(CompanionTheme.ink)
    }

    private var welcome: some View {
        ScrollView {
            VStack(spacing: 20) {
                Image(systemName: "desktopcomputer.and.iphone").font(.system(size: 38, weight: .light))
                    .foregroundStyle(CompanionTheme.secondary).padding(.top, 60)
                VStack(spacing: 8) {
                    Text(model.isConnected ? "Your chats, here" : "Your Mac, within reach").font(.title2.weight(.semibold))
                    Text(model.isConnected ? "Start a chat here or on your Mac. It appears in both." : "Continue a conversation, check on work and answer approvals from your phone.")
                        .font(.callout).foregroundStyle(CompanionTheme.secondary).multilineTextAlignment(.center)
                }
                if model.isConnected { Button("New chat") { creatingChat = true }.buttonStyle(.borderedProminent) }
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
            HStack { Spacer(); Button("Deny") { model.decide(false) }.buttonStyle(.bordered).keyboardShortcut("d", modifiers: .command)
                Button("Allow once") { model.decide(true) }.buttonStyle(.borderedProminent).tint(CompanionTheme.ink).foregroundStyle(CompanionTheme.surface)
                    .keyboardShortcut("y", modifiers: .command) }
                .disabled(!model.isConnected)
        }.padding(14).companionCard(cornerRadius: CompanionTheme.panelRadius, accent: CompanionTheme.manual)
            .padding(.horizontal, 14).padding(.bottom, 8)
    }

    private func composer(_ chat: RemoteChat) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            if !model.composeNotice.isEmpty { Text(model.composeNotice).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
            if !model.uploadProgress.isEmpty { Text(model.uploadProgress).font(.caption).foregroundStyle(CompanionTheme.secondary) }
            if let files = model.attachmentDrafts[chat.id], !files.isEmpty {
                ScrollView(.horizontal) {
                    HStack {
                        ForEach(files) { file in
                            HStack(spacing: 6) {
                                Image(systemName: "paperclip")
                                Text(file.name).lineLimit(1)
                                Button { model.removeAttachment(file.id, chat: chat.id) } label: { Image(systemName: "xmark.circle.fill") }
                                    .buttonStyle(.plain).accessibilityLabel("Remove \(file.name)").disabled(model.attachmentSending)
                            }.font(.caption).padding(8).companionCard(cornerRadius: 8)
                        }
                    }
                }
            }
            // A plain reconnect is already said by the header and placeholder;
            // anything with a reason (an error, a pairing step) keeps it here.
            if model.store.needsSnapshot || model.connectionPhase != nil && !["Connecting…", "Disconnected. Reconnecting…"].contains(model.status) {
                Text(model.store.needsSnapshot ? "Refreshing after a missed update…" : model.status)
                    .font(.caption).foregroundStyle(CompanionTheme.secondary).lineLimit(2)
            } else if model.isConnected, let notice = model.store.notices.last {
                Text(notice).font(.caption).foregroundStyle(CompanionTheme.secondary).lineLimit(2)
            }
            HStack(alignment: .bottom, spacing: 10) {
                if model.extendedControlsAvailable {
                    Menu {
                        Button("Attach files…") { importingFiles = true }
                        #if os(iOS)
                        Button { showingPhotos = true } label: { Label("Photo library", systemImage: "photo") }
                        #endif
                    } label: { Image(systemName: "plus").frame(width: 32, height: 40) }
                        .disabled(model.attachmentSending).accessibilityLabel("Add attachment")
                }
                CompanionMentionField(text: $model.draft, placeholder: placeholder(chat),
                    enabled: model.isConnected && !model.store.needsSnapshot,
                    mentions: model.mentionTargets, menu: mentionMenu)
                    .accessibilityLabel("Message to \(chat.title)")
                Button { if model.runIsOpen { model.stop() } else { model.sendDraft() } } label: {
                    Image(systemName: model.runIsOpen ? "stop.fill" : "arrow.up")
                        .font(.system(size: 16, weight: .bold)).foregroundStyle(CompanionTheme.surface)
                        .frame(width: 36, height: 36).background(CompanionTheme.ink, in: Circle()).padding(.vertical, 4)
                }.buttonStyle(.plain).disabled(!model.isConnected || !model.runIsOpen && !model.canSend)
                    .opacity(!model.runIsOpen && !model.canSend ? 0.4 : 1)
                    .keyboardShortcut(model.runIsOpen ? KeyboardShortcut(".", modifiers: .command) : KeyboardShortcut(.return, modifiers: .command))
                    .accessibilityLabel(model.runIsOpen ? "Stop this chat" : "Send message")
            }.padding(.horizontal, 14).padding(.vertical, 4)
                .overlay(alignment: .topLeading) {
                    CompanionMentionList(menu: mentionMenu, models: model.models, accent: chat.providerAccent)
                        .padding(.leading, 46)
                        .alignmentGuide(.top) { $0[.bottom] + 6 }
                }
                .companionCard(cornerRadius: 20, accent: model.approvalMode == "yolo" ? .red : chat.providerAccent)
            footer
        }.padding(.horizontal, 14).padding(.top, 8).padding(.bottom, 10)
    }

    private func placeholder(_ chat: RemoteChat) -> String {
        if let phase = model.connectionPhase { return phase == "Connecting" ? "Connecting…" : "Offline" }
        if model.team["enabled"].flag { return model.mentionTargets.isEmpty ? "Message the Team…" : "Message the Team, or @ a member…" }
        return "Message " + chat.displayModel + "…"
    }

    /// The approval mode, the chat's status and the turn clock, as on the
    /// Mac. A Team leaves what is running to the activity strip above.
    private var footer: some View {
        let mode = model.approvalMode
        return HStack(spacing: 6) {
            Circle().fill(CompanionTheme.approvalColor(mode)).frame(width: 5, height: 5)
            Menu {
                ForEach(["manual", "accept_edits", "yolo"], id: \.self) { option in
                    Button { choosePermission(option) } label: {
                        if option == mode { Label(CompanionTheme.approvalLabel(option), systemImage: "checkmark") }
                        else { Text(CompanionTheme.approvalLabel(option)) }
                    }
                }
            } label: { HStack(spacing: 3) { Text(CompanionTheme.approvalLabel(mode)); Image(systemName: "chevron.down").font(.system(size: 8)) } }
                .foregroundStyle(CompanionTheme.approvalColor(mode)).disabled(!model.configurable)
                .accessibilityLabel("Change chat permissions")
            if !model.team["enabled"].flag, let chat = model.selectedChat {
                let status = model.selectionBound && !model.store.selectedStatus.isEmpty ? model.store.selectedStatus : chat.status
                ChatSharedShimmerText(text: status.isEmpty ? (model.runIsOpen ? "Working…" : "Ready") : status,
                    active: model.runIsOpen && model.store.approval == nil, accent: chat.providerAccent, base: CompanionTheme.secondary)
                    .lineLimit(1)
            }
            Spacer(minLength: 8)
            CompanionTurnTime(startedAt: model.turnStartedAt)
        }.font(.caption2).padding(.horizontal, 5)
    }

    private var settingsSheet: some View {
        NavigationStack {
            Form {
                Section("Your Mac") {
                    LabeledContent("Connection", value: model.isConnected ? "Connected" : "Offline")
                    let host = model.store.controls["options"]?["host"] ?? .null
                    LabeledContent("Mac host", value: host["version"].text.isEmpty ? "Older host — update required for all controls" : host["version"].text + " (" + host["build"].text + ")")
                    LabeledContent("iOS companion", value: (Bundle.main.object(forInfoDictionaryKey: "CFBundleShortVersionString") as? String ?? "Development") + " (" + (Bundle.main.object(forInfoDictionaryKey: "CFBundleVersion") as? String ?? "source") + ")")
                    if !model.extendedControlsAvailable { Text("Update the Mac host to use older messages, attachments, New Chat and Blackboard. Pairing is already saved; an update does not require pairing again.").font(.caption).foregroundStyle(.orange) }
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
