import SwiftUI
import UniformTypeIdentifiers

struct CompanionBlackboard: View {
    @ObservedObject var model: CompanionModel
    @Environment(\.dismiss) private var dismiss
    @State private var editing = false
    @State private var key = ""
    @State private var bodyText = ""
    @State private var category = "note"
    @State private var remove: RemoteValue?
    @State private var link = ""
    @State private var busy = false
    @State private var notice = ""
    @State private var importing = false
    private var board: RemoteValue { model.store.controls["blackboard"] ?? .null }
    private var categories: [String] { ["decision", "fact", "risk", "do-not-repeat", "note"] }

    var body: some View {
        NavigationStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 12) {
                    Text("Shared reference posts and files for this chat and its Team. Posts are reference material, not permissions.")
                        .font(.system(size: 11.5)).foregroundStyle(CompanionTheme.secondary)
                    HStack {
                        Text("\(board["posts"].rows.count) / \(board["limits"]["posts"].int) posts").font(.caption)
                        Spacer()
                        Button("New post") { key = ""; bodyText = ""; category = "note"; editing = true }
                    }
                    ForEach(board["posts"].rows, id: \.self) { post in
                        VStack(alignment: .leading, spacing: 6) {
                            HStack(spacing: 6) {
                                if !post["route"].text.isEmpty { CompanionProviderMark(presentation: CompanionBranding.presentation(route: post["route"].text, models: model.models), size: 14) }
                                Text(post["authorName"].text).font(.system(size: 12, weight: .medium))
                                Text(post["category"].text).font(.system(size: 10.5)).foregroundStyle(CompanionTheme.secondary)
                                Spacer()
                                Menu {
                                    if post["author"].text == "user" { Button("Edit") { key = post["key"].text; bodyText = post["body"].text; category = post["category"].text; editing = true } }
                                    Button("Remove", role: .destructive) { remove = post }
                                } label: { Image(systemName: "ellipsis").frame(width: 32, height: 28) }.accessibilityLabel("Post actions")
                            }
                            Text(post["key"].text).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(CompanionTheme.secondary)
                            Text(post["body"].text).font(.system(size: 12)).textSelection(.enabled)
                            if !post["quote"]["excerpt"].text.isEmpty { Text(post["quote"]["excerpt"].text).font(.caption).foregroundStyle(CompanionTheme.secondary).padding(.leading, 8) }
                        }.padding(10).companionCard(cornerRadius: 7)
                    }
                    Divider()
                    Text("Files and links").font(.system(size: 13, weight: .semibold))
                    ForEach(board["attachments"].rows, id: \.self) { file in
                        HStack {
                            Image(systemName: file["kind"].text == "url" ? "link" : "paperclip")
                            if let url = URL(string: file["url"].text), ["http", "https"].contains(url.scheme ?? "") { Link(file["name"].text.isEmpty ? url.host ?? "Link" : file["name"].text, destination: url) }
                            else { Text(file["name"].text).textSelection(.enabled) }
                            Spacer()
                            if file["source"].text == "board" { Button(role: .destructive) { remove = file } label: { Image(systemName: "trash").frame(width: 32, height: 32) }.accessibilityLabel("Remove \(file["name"].text)") }
                        }.font(.system(size: 11.5))
                    }
                    HStack {
                        TextField("https://…", text: $link).textFieldStyle(.roundedBorder)
                        Button("Add link") { run("blackboard_attach", ["url": .string(link)]) { link = "" } }.disabled(link.isEmpty)
                    }
                    Button("Attach files…") { importing = true }
                    if let files = model.attachmentDrafts[model.selectedChatID ?? ""], !files.isEmpty {
                        Text(files.map(\.name).joined(separator: ", ")).font(.caption)
                        Button("Pin attached files") { Task { @MainActor in await model.sendWithAttachments(toBlackboard: true) } }
                    }
                    if !notice.isEmpty { Text(notice).font(.caption).foregroundStyle(.red).textSelection(.enabled) }
                    if !model.composeNotice.isEmpty { Text(model.composeNotice).font(.caption).foregroundStyle(.red) }
                    if !model.uploadProgress.isEmpty { Text(model.uploadProgress).font(.caption) }
                }.padding(14)
            }.background(CompanionTheme.surface).foregroundStyle(CompanionTheme.ink)
            .navigationTitle("Blackboard").navigationBarTitleDisplayModeCompatible()
            .toolbar {
                ToolbarItem(placement: .cancellationAction) { Button("Done") { dismiss() } }
                ToolbarItem(placement: .primaryAction) { Button { run("blackboard") } label: { Image(systemName: "arrow.clockwise") }.accessibilityLabel("Refresh Blackboard") }
            }
            .disabled(busy || !model.isConnected || model.attachmentSending)
            .task { run("blackboard") }
            .sheet(isPresented: $editing) {
                NavigationStack {
                    Form {
                        TextField("Key (letters, digits, dots or hyphens)", text: $key)
                        Picker("Category", selection: $category) { ForEach(categories, id: \.self) { Text($0.capitalized).tag($0) } }
                        TextField("Post", text: $bodyText, axis: .vertical).lineLimit(4...12)
                        Text("\(bodyText.utf8.count) / 1,500 bytes").font(.caption).foregroundStyle(CompanionTheme.secondary)
                        if !notice.isEmpty { Text(notice).font(.caption).foregroundStyle(.red) }
                    }.navigationTitle("Pin a post")
                    .toolbar {
                        ToolbarItem(placement: .cancellationAction) { Button("Cancel") { editing = false } }
                        ToolbarItem(placement: .confirmationAction) { Button("Save") { run("blackboard_post", ["key": .string(key), "body": .string(bodyText), "category": .string(category)]) { editing = false } }.disabled(busy || key.isEmpty || bodyText.isEmpty || bodyText.utf8.count > 1500) }
                    }
                }
            }
            .confirmationDialog("Remove this Blackboard item?", isPresented: Binding(get: { remove != nil }, set: { if !$0 { remove = nil } })) {
                Button("Remove", role: .destructive) {
                    if let value = remove {
                        run(value["body"] == .null ? "blackboard_detach" : "blackboard_remove", [value["body"] == .null ? "attachment" : "post": value["id"]]) { remove = nil }
                    }
                }
            }
            .fileImporter(isPresented: $importing, allowedContentTypes: [.data], allowsMultipleSelection: true) { result in
                do {
                    for url in try result.get() {
                        let access = url.startAccessingSecurityScopedResource()
                        defer { if access { url.stopAccessingSecurityScopedResource() } }
                        guard (try url.resourceValues(forKeys: [.fileSizeKey]).fileSize ?? 0) <= 8 * 1024 * 1024 else { throw RemoteControlFailure.rejected("Choose files under 8 MiB") }
                        try model.addAttachment(name: url.lastPathComponent, data: Data(contentsOf: url))
                    }
                } catch { notice = error.localizedDescription }
            }
        }
    }
    private func run(_ action: String, _ args: [String: RemoteValue] = [:], success: @escaping () -> Void = {}) {
        busy = action != "blackboard"; notice = ""
        Task { @MainActor in
            do { _ = try await model.performControl(action, args); success() }
            catch { notice = error.localizedDescription }
            busy = false
        }
    }
}

private extension View {
    @ViewBuilder func navigationBarTitleDisplayModeCompatible() -> some View {
        #if os(iOS)
        navigationBarTitleDisplayMode(.inline)
        #else
        self
        #endif
    }
}
