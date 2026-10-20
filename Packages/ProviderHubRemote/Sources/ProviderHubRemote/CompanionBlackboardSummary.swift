import SwiftUI

struct CompanionPostCard: View {
    @ObservedObject var model: CompanionModel
    var post: RemoteValue
    var body: some View {
        let brand = CompanionBranding.presentation(route: post["route"].text, models: model.models)
        ChatSharedBlackboardPost(author: post["authorName"].text, key: post["key"].text, text: post["body"].text,
            category: post["category"].text, quote: post["quote"]["excerpt"].text,
            quoteAuthor: post["quote"]["author"].text + " · " + post["quote"]["kind"].text,
            updated: ISO8601DateFormatter().date(from: post["updated"].text),
            accent: post["author"].text == "user" ? CompanionTheme.ink : brand.accent,
            ink: CompanionTheme.ink, secondary: CompanionTheme.secondary, fill: CompanionTheme.cardFill, rim: CompanionTheme.cardRim) {
                if post["author"].text == "user" { Image(systemName: "person.fill").font(.system(size: 8)) }
                else { CompanionProviderMark(presentation: brand, size: 10) }
            }
    }
}

struct CompanionBlackboardSummary: View {
    @ObservedObject var model: CompanionModel
    var expand: () -> Void
    @State private var bodyText = ""
    @State private var category = "note"
    @State private var posting = false
    @State private var notice = ""
    private var board: RemoteValue { model.store.controls["blackboard"] ?? .null }
    /// Files open from the Mac, so they need a chat; links need a web address.
    private var attachments: [RemoteValue] {
        board["attachments"].rows.filter { Self.link($0) != nil || ($0["url"].text.isEmpty && model.selectedChatID != nil) }
    }
    private static func link(_ file: RemoteValue) -> URL? {
        guard !file["url"].text.isEmpty, let url = URL(string: file["url"].text), ["http", "https"].contains(url.scheme ?? "") else { return nil }
        return url
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Divider()
            HStack(spacing: 5) {
                Text("Blackboard").font(.system(size: 12, weight: .semibold))
                Text("\(board["posts"].rows.count)/48").font(.system(size: 10.5)).foregroundStyle(.secondary)
                Spacer()
                Button(action: expand) { Image(systemName: "arrow.up.left.and.arrow.down.right").font(.system(size: 11)).frame(width: 28, height: 28) }.buttonStyle(.plain).accessibilityLabel("Expand Blackboard")
            }
            Text("Pinned facts, decisions and files every member on your team can refer to. Posts are references, not verified facts.")
                .font(.system(size: 10.5)).foregroundStyle(.secondary)
            ForEach(Array(board["posts"].rows.sorted { $0["updated"].text > $1["updated"].text }.prefix(8)), id: \.self) { post in CompanionPostCard(model: model, post: post) }
            if board["posts"].rows.count > 8 { Button("Show all \(board["posts"].rows.count) posts", action: expand).font(.system(size: 10.5)) }
            if !attachments.isEmpty {
                Text("Attachments · \(attachments.count)").font(.system(size: 10.5)).foregroundStyle(.secondary)
                ChatSharedBlackboardTiles(items: attachments, id: \.self) { file in
                    if let url = Self.link(file) { CompanionLinkTile(file: file, url: url) }
                    else if let chat = model.selectedChatID { CompanionFileTile(model: model, file: file, chat: chat) }
                }
                if attachments.count > ChatSharedBlackboardTileLayout.limit { Button("Show all \(attachments.count) attachments", action: expand).font(.system(size: 10.5)) }
            }
            HStack {
                TextField("Pin a fact, decision or risk for the Team…", text: $bodyText, axis: .vertical).textFieldStyle(.roundedBorder).font(.system(size: 11.5))
                Picker("Category", selection: $category) { ForEach(["note", "fact", "decision", "risk", "do-not-repeat"], id: \.self) { Text($0.capitalized).tag($0) } }.labelsHidden().fixedSize()
            }
            HStack {
                Button(action: expand) { Image(systemName: "paperclip") }.accessibilityLabel("Attach to Blackboard")
                Button(action: expand) { Image(systemName: "link") }.accessibilityLabel("Add Blackboard link")
                Spacer()
                Button(posting ? "Posting…" : "Post") {
                    let text = bodyText
                    posting = true
                    Task { @MainActor in
                        do {
                            _ = try await model.performControl("blackboard_post", ["key": .string("phone-" + UUID().uuidString.lowercased()), "body": .string(text), "category": .string(category)])
                            if bodyText == text { bodyText = "" }; notice = ""
                        } catch { notice = error.localizedDescription }
                        posting = false
                    }
                }.disabled(posting || bodyText.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty || bodyText.utf8.count > 1500)
            }.buttonStyle(.plain).font(.system(size: 11))
            if !notice.isEmpty { Text(notice).font(.caption).foregroundStyle(.red) }
        }.task(id: model.selectedChatID) { model.control("blackboard") }
    }
}

/// A link in the inspector's tile rows, the same size as CompanionFileTile.
private struct CompanionLinkTile: View {
    var file: RemoteValue
    var url: URL
    private var name: String { file["name"].text.isEmpty ? url.host ?? "Link" : file["name"].text }
    var body: some View {
        Link(destination: url) {
            VStack(alignment: .leading, spacing: 3) {
                ZStack {
                    RoundedRectangle(cornerRadius: 5).fill(CompanionTheme.raised)
                    Image(systemName: "link").foregroundStyle(CompanionTheme.secondary)
                }.frame(width: 108, height: 64).clipShape(RoundedRectangle(cornerRadius: 5))
                Text(name).font(.system(size: 10.5)).foregroundStyle(CompanionTheme.ink).lineLimit(1).truncationMode(.middle)
                Text([file["authorName"].text, url.host ?? ""].filter { !$0.isEmpty }.joined(separator: " · "))
                    .font(.system(size: 9.5)).foregroundStyle(CompanionTheme.secondary).lineLimit(1)
            }.frame(width: 108).contentShape(Rectangle())
        }.buttonStyle(.plain).accessibilityLabel("Open link " + name)
    }
}
