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
            Text("Attachments").font(.system(size: 10.5)).foregroundStyle(.secondary)
            ScrollView(.horizontal) {
                HStack(alignment: .top, spacing: 8) {
                    ForEach(board["attachments"].rows, id: \.self) { file in
                        if file["url"].text.isEmpty, let chat = model.selectedChatID { CompanionFileTile(model: model, file: file, chat: chat) }
                        else if let url = URL(string: file["url"].text), ["http", "https"].contains(url.scheme ?? "") { Link(file["name"].text.isEmpty ? "Link" : file["name"].text, destination: url).font(.caption) }
                    }
                }
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
