import SwiftUI

/// Adapts the bounded wire entries to the exact outline used by macOS.
struct RemoteTranscriptItem: ChatTranscriptItem {
    let entry: RemoteEntry
    var id: String { entry.id }
    var time: Double? { entry.time }
    var kind: String { entry.role }
    var text: String { entry.text }
    var route: String { entry.route }
    var tool: String? { entry.tool.isEmpty ? nil : entry.tool }
    var summary: String? { entry.summary.isEmpty ? nil : entry.summary }
    var detail: String? { entry.detail.isEmpty ? nil : entry.detail }
    var isError: Bool { entry.isError }
    var memberID: String? { entry.memberID.isEmpty ? (entry.memberName.isEmpty ? nil : entry.memberName) : entry.memberID }
    var memberName: String? { entry.memberName.isEmpty ? nil : entry.memberName }
    var agentID: String? { entry.agentID.isEmpty ? nil : entry.agentID }
    var agentIDs: [String]? { nil }
    var changedFiles: [String] { entry.changedFiles }
}

struct CompanionTranscript: View {
    let entries: [RemoteEntry]
    let chat: RemoteChat
    var connected = false
    var models: [RemoteValue] = []
    var members: [RemoteValue] = []
    var fallbackRoute = ""
    var hasOlder = false
    var loadingOlder = false
    var loadOlder: () -> Void = {}
    var answer: (RemoteQuestion, [String: [String]], Bool) -> Void = { _, _, _ in }
    @State private var followsLatest = true
    @State private var expanded = Set<String>()
    @ScaledMetric(relativeTo: .body) private var textSize: CGFloat = 13

    private var scale: CGFloat { textSize / 13 }
    private var items: [RemoteTranscriptItem] { entries.map(RemoteTranscriptItem.init) }
    private var outline: [ChatTranscriptSegment<RemoteTranscriptItem>] {
        ChatTranscriptSegment.outline(items, isLive: { $0.entry.isLive })
    }

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 0) {
                    if hasOlder {
                        Button(action: loadOlder) {
                            HStack { if loadingOlder { ProgressView().controlSize(.small) }; Text(loadingOlder ? "Loading older messages…" : "Load earlier messages") }
                        }.buttonStyle(.plain).font(.system(size: 12 * scale)).frame(maxWidth: .infinity).padding(.bottom, 16).disabled(loadingOlder)
                    }
                    ForEach(outline) { segment in
                        Group {
                            switch segment.content {
                            case .speaker(let item): speaker(item)
                            case .entry(let item): row(item)
                            case .fold(let items):
                                DisclosureGroup(isExpanded: expandedBinding(segment.id)) {
                                    VStack(alignment: .leading, spacing: 6 * scale) { ForEach(items, id: \.id) { row($0) } }.padding(.top, 6 * scale)
                                } label: {
                                    HStack(spacing: 7 * scale) {
                                        Text("Worked").font(.system(size: 12 * scale, weight: .medium))
                                        Text(ChatToolDisplay.activity(items.map(\.tool))).font(.system(size: 11.5 * scale)).foregroundStyle(CompanionTheme.secondary)
                                    }
                                }.tint(CompanionTheme.secondary)
                            case .changes(let items): changes(items)
                            }
                        }.padding(.top, gap(segment.spacing)).id(segment.id)
                    }
                    if chat.busy && entries.last?.isLive != true {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small).tint(chat.providerAccent)
                            Text("Thinking…").font(.system(size: textSize)).foregroundStyle(CompanionTheme.secondary)
                        }.padding(.top, 6 * scale)
                    }
                    Color.clear.frame(height: 36 * scale).id("transcript-bottom")
                        .onAppear { followsLatest = true }.onDisappear { followsLatest = false }
                }
                .frame(maxWidth: 760 * scale).frame(maxWidth: .infinity)
                .padding(.horizontal, 20).padding(.top, 16).padding(.bottom, 4)
            }
            .onAppear { proxy.scrollTo("transcript-bottom", anchor: .bottom) }
            .onChange(of: entries) { old, new in
                if let first = old.first, new.first?.id != first.id, new.contains(where: { $0.id == first.id }) {
                    followsLatest = false; proxy.scrollTo(first.id, anchor: .top)
                } else if followsLatest { proxy.scrollTo("transcript-bottom", anchor: .bottom) }
            }
            .overlay(alignment: .bottomTrailing) {
                if !followsLatest {
                    Button { followsLatest = true; proxy.scrollTo("transcript-bottom", anchor: .bottom) } label: {
                        Image(systemName: "arrow.down").padding(12).background(.regularMaterial, in: Circle())
                    }.buttonStyle(.plain).padding(12).accessibilityLabel("Go to latest message")
                }
            }
        }
    }

    private func presentation(_ item: RemoteTranscriptItem) -> CompanionPresentation {
        let member = members.first { $0["id"].text == item.memberID || $0["name"].text == item.memberName }
        let memberRoute = member?["route"].text ?? ""
        let chosenRoute = String((member?["choice"].text ?? "").split(separator: "|").first ?? "")
        let fallback = !memberRoute.isEmpty ? memberRoute : !chosenRoute.isEmpty ? chosenRoute : chat.route ?? fallbackRoute
        let route = item.route.isEmpty ? fallback : item.route
        return CompanionBranding.presentation(route: route, label: member?["label"].text ?? chat.displayModel, models: models)
    }
    private func speaker(_ item: RemoteTranscriptItem) -> some View {
        let brand = presentation(item)
        return ChatSharedSpeakerHeader(member: item.memberName, label: brand.label, timestamp: ChatSectionTime.label(item.time),
            accent: brand.accent, ink: CompanionTheme.ink, secondary: CompanionTheme.secondary, live: item.entry.isLive, scale: scale) {
                CompanionProviderMark(presentation: brand, size: 15 * scale)
            }
    }
    @ViewBuilder private func row(_ item: RemoteTranscriptItem) -> some View {
        let entry = item.entry
        let brand = presentation(item)
        switch entry.role {
        case "question":
            if let question = entry.question {
                CompanionQuestionCard(question: question, accent: brand.accent, enabled: connected) { values, dismiss in answer(question, values, dismiss) }
            }
        case "user":
            VStack(alignment: .trailing, spacing: 6) {
                attachmentStrip(entry)
                HStack {
                Spacer(minLength: 56 * scale)
                (Text("User: ").bold() + Text(entry.text)).font(.system(size: textSize)).textSelection(.enabled)
                    .padding(.horizontal, 13 * scale).padding(.vertical, 8 * scale)
                    .background(RoundedRectangle(cornerRadius: 15 * scale, style: .continuous).fill(CompanionTheme.selection))
                }
            }.accessibilityLabel("You: " + entry.text)
        case "assistant":
            let reply = ChatReplySources.split(entry.text)
            VStack(alignment: .leading, spacing: 8 * scale) {
                if ChatReplySources.isBlank(reply.body), entry.isLive {
                    Text("Thinking…").font(.system(size: textSize)).foregroundStyle(brand.accent)
                } else { CompanionMessageText(text: reply.body, size: textSize) }
                if !reply.links.isEmpty {
                    HStack { ForEach(reply.links) { link in if let url = URL(string: link.url) { Link(link.title, destination: url).font(.system(size: 11 * scale)) } } }
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        case "tool": tool(item, brand: brand)
        default:
            Text(entry.text).font(.system(size: 11.5 * scale)).foregroundStyle(entry.isError ? .red : CompanionTheme.secondary).textSelection(.enabled)
        }
    }
    private func tool(_ item: RemoteTranscriptItem, brand: CompanionPresentation) -> some View {
        let display = ChatToolDisplay.describe(tool: item.tool, summary: item.summary ?? item.text, live: item.entry.isLive)
        return VStack(alignment: .leading, spacing: 6 * scale) {
            Button { withAnimation(.easeInOut(duration: 0.15)) { toggle(item.id) } } label: {
                HStack(spacing: 7 * scale) {
                    ChatToolGlyph(name: ChatToolDisplay.glyph(item.tool), size: 15 * scale).foregroundStyle(brand.accent)
                    Text(display.verb).font(.system(size: 12 * scale, weight: .medium)).fixedSize()
                    if !display.subject.isEmpty {
                        Text(display.subject).font(.system(size: 11.5 * scale, design: display.code ? .monospaced : .default))
                            .foregroundStyle(CompanionTheme.secondary).lineLimit(1).truncationMode(.middle)
                    }
                    Spacer(minLength: 0)
                    if item.isError { Image(systemName: "exclamationmark.triangle").foregroundStyle(.red) }
                    Image(systemName: "chevron.right").font(.system(size: 9 * scale)).rotationEffect(.degrees(expanded.contains(item.id) ? 90 : 0)).foregroundStyle(CompanionTheme.secondary)
                }.contentShape(Rectangle())
            }.buttonStyle(.plain)
            if expanded.contains(item.id) {
                ScrollView(.horizontal) { ChatSharedPatchText(text: item.detail ?? item.text, size: 11.5 * scale, ink: CompanionTheme.ink, secondaryInk: CompanionTheme.secondary) }
                    .padding(10).companionCard(cornerRadius: 8, accent: brand.accent)
            }
        }
    }
    private func changes(_ items: [RemoteTranscriptItem]) -> some View {
        let changes = ChatTurnChanges.collect(items)
        return VStack(alignment: .leading, spacing: 6 * scale) {
            Text("Changed \(changes.files.count) files · +\(changes.added) −\(changes.removed)").font(.system(size: 11.5 * scale, weight: .medium))
            ForEach(changes.files) { file in
                HStack { Text(file.path).lineLimit(1); Spacer(); Text(file.members.joined(separator: ", ")).foregroundStyle(CompanionTheme.secondary) }.font(.system(size: 11 * scale))
            }
        }.padding(10).companionCard(cornerRadius: 8)
    }
    private func attachmentStrip(_ entry: RemoteEntry) -> some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack {
                ForEach(entry.attachments) { file in
                    Label(file.name + " · " + ByteCountFormatter.string(fromByteCount: Int64(file.size), countStyle: .file), systemImage: file.kind == "image" ? "photo" : "doc")
                        .font(.system(size: 11 * scale)).padding(8).companionCard(cornerRadius: 8)
                }
            }
        }.frame(height: entry.attachments.isEmpty ? 0 : nil)
    }
    private func gap(_ spacing: ChatTranscriptSegment<RemoteTranscriptItem>.Spacing) -> CGFloat {
        switch spacing { case .none: return 0; case .speaker: return 26 * scale; case .header, .item: return 6 * scale }
    }
    private func toggle(_ id: String) { if expanded.contains(id) { expanded.remove(id) } else { expanded.insert(id) } }
    private func expandedBinding(_ id: String) -> Binding<Bool> {
        Binding(get: { expanded.contains(id) }, set: { value in if value { expanded.insert(id) } else { expanded.remove(id) } })
    }
}

/// Native selectable text on iOS, with the desktop's reading size and code cards.
private struct CompanionMessageText: View {
    let text: String
    var size: CGFloat = 13
    var body: some View {
        let blocks = text.components(separatedBy: "```")
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { index, block in
                if index % 2 == 1 {
                    let code = block.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
                    VStack(alignment: .leading, spacing: 6) {
                        if let language = code.first, !language.isEmpty { Text(language).font(.system(size: size * 0.81)).foregroundStyle(CompanionTheme.secondary) }
                        ScrollView(.horizontal) { Text(code.count > 1 ? String(code[1]) : block).font(.system(size: size, design: .monospaced)).textSelection(.enabled) }
                    }.padding(12).companionCard(cornerRadius: 8)
                } else if !block.isEmpty {
                    ForEach(Array(block.components(separatedBy: "\n\n").enumerated()), id: \.offset) { _, paragraph in
                        Text((try? AttributedString(markdown: paragraph, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(paragraph))
                            .font(.system(size: size)).textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                    }
                }
            }
        }
    }
}
