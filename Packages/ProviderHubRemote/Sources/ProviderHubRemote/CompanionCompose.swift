import CryptoKit
import Foundation
import SwiftUI
import UniformTypeIdentifiers
#if os(iOS)
import PhotosUI
import UIKit
#endif

public struct RemoteAttachment: Codable, Equatable, Identifiable {
    public var id: String
    public var name: String
    public var kind: String
    public var size: Int
}

public struct RemoteDraftAttachment: Equatable, Identifiable {
    public var id = UUID().uuidString.replacingOccurrences(of: "-", with: "").lowercased()
    public var name: String
    public var data: Data
}

enum RemoteControlFailure: LocalizedError {
    case rejected(String)
    var errorDescription: String? { if case .rejected(let text) = self { return text }; return nil }
}

extension CompanionModel {
    public var extendedControlsAvailable: Bool { (store.controls["extensionVersion"]?.int ?? 0) >= 2 }
    public var remoteWorkspaces: [RemoteValue] { store.controls["options"]?["workspaces"].rows ?? [] }

    @MainActor
    public func performControl(_ action: String, _ arguments: [String: RemoteValue] = [:], chat: String? = nil) async throws -> RemoteValue {
        guard isConnected else { throw RemoteControlFailure.rejected("Reconnect to your Mac first") }
        let id = UUID().uuidString
        var command = RemoteCommand(method: "chat.control", requestId: id, chat: chat ?? selectedChatID)
        command.action = action; command.arguments = arguments
        return try await withCheckedThrowingContinuation { continuation in
            pendingControls[id] = continuation
            link?.send(command)
            Task { @MainActor [weak self] in
                try? await Task.sleep(nanoseconds: 30_000_000_000)
                self?.pendingControls.removeValue(forKey: id)?.resume(throwing: RemoteControlFailure.rejected("The Mac did not acknowledge this action. Reconnect and inspect before retrying."))
            }
        }
    }

    @MainActor
    public func refreshRemoteOptions() async {
        guard (store.controls["extensionVersion"]?.int ?? 0) >= 2 else { return }
        do {
            let options = try await performControl("remote_options")
            if options["version"].int >= 3 {
                var rows: [RemoteValue] = []
                var offset = 0
                while true {
                    let page = try await performControl("remote_models", ["offset": .number(Double(offset))])
                    rows += page["models"].rows
                    guard page["nextOffset"] != .null else { break }
                    guard page["nextOffset"].int > offset else { throw RemoteControlFailure.rejected("Model catalogue changed; refresh again") }
                    offset = page["nextOffset"].int
                }
                fullModelCatalogue = rows
            }
        } catch { composeNotice = error.localizedDescription }
    }

    @MainActor
    public func createRemoteChat(choice: String, workspace: String, effort: String = "") async throws {
        let result = try await performControl("remote_create", ["choice": .string(choice), "workspace": .string(workspace), "effort": .string(effort)])
        let id = result["chat"].text
        guard !id.isEmpty else { throw RemoteControlFailure.rejected("The Mac did not return a new chat") }
        selectedChatID = id
        select(id)
    }

    @MainActor
    public func loadOlder() async {
        guard !historyLoading, store.hasOlder, let first = store.visibleEntries.first else { return }
        historyLoading = true
        defer { historyLoading = false }
        do { _ = try await performControl("remote_history", ["before": .string(first.id)]) }
        catch { composeNotice = error.localizedDescription }
    }

    public func addAttachment(name: String, data: Data) throws {
        guard let chat = selectedChatID else { throw RemoteControlFailure.rejected("Choose a chat first") }
        let current = attachmentDrafts[chat] ?? []
        guard !data.isEmpty, data.count <= 8 * 1024 * 1024, current.count < 8,
              current.reduce(data.count, { $0 + $1.data.count }) <= 20 * 1024 * 1024 else {
            throw RemoteControlFailure.rejected("Attach up to eight files, 8 MiB each and 20 MiB total")
        }
        attachmentDrafts[chat] = current + [RemoteDraftAttachment(name: name, data: data)]
    }

    public func removeAttachment(_ id: String, chat: String) {
        attachmentDrafts[chat]?.removeAll { $0.id == id }
        if isConnected && chat == selectedChatID {
            Task { @MainActor in _ = try? await performControl("remote_upload_drop", ["upload": .string(id)], chat: chat) }
        }
    }

    @MainActor
    func upload(_ file: RemoteDraftAttachment, chat: String) async throws -> String {
        let digest = SHA256.hash(data: file.data).map { String(format: "%02x", $0) }.joined()
        let begin = try await performControl("remote_upload_begin", ["upload": .string(file.id), "name": .string(file.name),
            "size": .number(Double(file.data.count)), "sha256": .string(digest)], chat: chat)
        var offset = begin["offset"].int
        guard offset >= 0, offset <= file.data.count else { throw RemoteControlFailure.rejected("Invalid upload offset") }
        while offset < file.data.count {
            try Task.checkCancellation()
            let end = min(file.data.count, offset + 18 * 1024)
            uploadProgress = "Uploading \(file.name) · \(offset * 100 / file.data.count)%"
            let reply = try await performControl("remote_upload_chunk", ["upload": .string(file.id), "offset": .number(Double(offset)),
                "data": .string(file.data.subdata(in: offset..<end).base64EncodedString())], chat: chat)
            guard reply["offset"].int == end else { throw RemoteControlFailure.rejected("The Mac rejected an attachment chunk") }
            offset = end
        }
        _ = try await performControl("remote_upload_finish", ["upload": .string(file.id)], chat: chat)
        return file.id
    }

    @MainActor
    public func sendWithAttachments(toBlackboard: Bool = false) async {
        guard !attachmentSending, let chat = selectedChatID else { return }
        let files = attachmentDrafts[chat] ?? []
        guard !files.isEmpty else { return }
        let text = draft
        attachmentSending = true; composeNotice = ""
        defer { attachmentSending = false; uploadProgress = "" }
        do {
            var tokens: [RemoteValue] = []
            for file in files { tokens.append(.string(try await upload(file, chat: chat))) }
            _ = try await performControl(toBlackboard ? "remote_board_attach" : "remote_send",
                toBlackboard ? ["uploads": .array(tokens)] : ["uploads": .array(tokens), "text": .string(text)], chat: chat)
            let sent = Set(files.map(\.id))
            attachmentDrafts[chat]?.removeAll { sent.contains($0.id) }
            if !toBlackboard, selectedChatID == chat && draft == text { draft = "" }
        } catch { composeNotice = error.localizedDescription }
    }
}

struct CompanionNewChat: View {
    @ObservedObject var model: CompanionModel
    @Environment(\.dismiss) private var dismiss
    @State private var choice = ""
    @State private var workspace = ""
    @State private var creating = false
    @State private var notice = ""
    @State private var effort = ""
    @State private var pickingModel = false
    var body: some View {
        NavigationStack {
            Form {
                Section("Model and account") {
                    Button { pickingModel = true } label: {
                        HStack { Text(model.models.first(where: { $0["id"].text == choice })?["label"].text ?? "Choose a model"); Spacer(); Text(effort).foregroundStyle(.secondary); Image(systemName: "chevron.right").font(.caption) }
                    }.buttonStyle(.plain)
                        .sheet(isPresented: $pickingModel) {
                            CompanionRoutePicker(model: model, initialChoiceID: choice, initialEffort: effort) { row, selectedEffort in
                                choice = row["id"].text; effort = selectedEffort; pickingModel = false
                            }
                        }
                }
                Section("Workspace on your Mac") {
                    Picker("Folder", selection: $workspace) {
                        Text("Choose a workspace").tag("")
                        ForEach(model.remoteWorkspaces, id: \.self) { folder in Text(folder["name"].text).tag(folder["path"].text) }
                    }
                    if !workspace.isEmpty { Text(workspace).font(.caption).textSelection(.enabled) }
                    Text("The new chat starts with Manual approvals. Execution and files stay on your Mac.").font(.caption)
                }
                if !model.extendedControlsAvailable { Text("Update the Mac host to enable creating chats remotely.") }
                if !notice.isEmpty { Text(notice).foregroundStyle(.red) }
            }
            .navigationTitle("New chat")
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Cancel") { dismiss() }.disabled(creating) }
                ToolbarItem(placement: .confirmationAction) {
                    Button(creating ? "Creating…" : "Create") {
                        creating = true
                        Task { @MainActor in
                            do { try await model.createRemoteChat(choice: choice, workspace: workspace, effort: effort); dismiss() }
                            catch { notice = error.localizedDescription }
                            creating = false
                        }
                    }.disabled(creating || choice.isEmpty || workspace.isEmpty || !model.extendedControlsAvailable)
                }
            }
        }.task {
            choice = model.configuration["choice"].text.isEmpty ? model.models.first?["id"].text ?? "" : model.configuration["choice"].text
            workspace = model.configuration["workspace"].text.isEmpty ? model.remoteWorkspaces.first?["path"].text ?? "" : model.configuration["workspace"].text
            await model.refreshRemoteOptions()
            if choice.isEmpty { choice = model.models.first?["id"].text ?? "" }
            if workspace.isEmpty { workspace = model.remoteWorkspaces.first?["path"].text ?? "" }
        }
    }
}
