import SwiftUI
#if os(iOS)
import UIKit
#endif

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

/// A recorded streaming flag describes the row's transport state, not whether
/// that model round is still active. Match desktop's current-entry selection
/// so empty placeholders from previous rounds cannot split a tool fold.
enum CompanionTranscriptState {
    static func liveEntryIDs(entries: [RemoteEntry], runIsOpen: Bool, activities: [CompanionActivity]) -> Set<String> {
        guard runIsOpen else { return [] }
        if !activities.isEmpty {
            return Set(activities.filter { $0.description.live && !$0.entryID.isEmpty }.map(\.entryID))
        }
        // Solo hosts without activity metadata can still identify their last
        // row. Team liveness requires the member's current contribution.
        guard let last = entries.last, last.memberID.isEmpty, last.memberName.isEmpty else { return [] }
        return [last.id]
    }
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
    var turnSummaries: [RemoteValue] = []
    var runIsOpen = false
    var activities: [CompanionActivity] = []
    var jumpEntryID: String?
    var jumpToken = 0
    /// The chat's folder and whether a Team runs it, for the empty-chat invitation.
    var workspace = ""
    var teamEnabled = false
    var answer: (RemoteQuestion, [String: [String]], Bool) -> Void = { _, _, _ in }
    @State private var followsLatest = true
    @State private var expanded = Set<String>()
    @State private var foldChoice: [String: Bool] = [:]
    /// The tail of the outline on screen, paged like the Mac's.
    @State private var visibleSegmentLimit = CompanionTranscript.page
    /// The first row on screen when older messages were asked of the Mac.
    @State private var olderAnchor: String?
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @ScaledMetric(relativeTo: .body) private var textSize: CGFloat = 13

    private static let page = 100
    private static let bottomID = "transcript-bottom"
    private var scale: CGFloat { textSize / 13 }
    private var items: [RemoteTranscriptItem] { entries.map(RemoteTranscriptItem.init) }
    private func outline(live: Set<String>) -> [ChatTranscriptSegment<RemoteTranscriptItem>] {
        ChatTranscriptSegment.outline(items, open: runIsOpen, isLive: { live.contains($0.id) })
    }
    private var liveEntryIDs: Set<String> {
        CompanionTranscriptState.liveEntryIDs(entries: entries, runIsOpen: runIsOpen, activities: activities)
    }
    private static func liveSpeakerIDs(_ outline: [ChatTranscriptSegment<RemoteTranscriptItem>], live: Set<String>) -> Set<String> {
        var result = Set<String>()
        var speaker: String?
        for segment in outline {
            switch segment.content {
            case .speaker: speaker = segment.id
            case .entry(let item):
                if live.contains(item.id), let speaker { result.insert(speaker) }
            case .fold(let items):
                if items.contains(where: { live.contains($0.id) }), let speaker { result.insert(speaker) }
            case .changes: speaker = nil
            }
        }
        return result
    }

    var body: some View {
        // One outline per update: every row reads these instead of rebuilding them.
        let live = liveEntryIDs
        let segments = outline(live: live)
        let visible = ChatTranscriptSegment.visibleTail(segments, limit: visibleSegmentLimit, liveEntryIDs: live)
        let liveSpeakers = Self.liveSpeakerIDs(segments, live: live)
        return ScrollViewReader { proxy in
            ScrollView {
                // Eager, not lazy: a lazy stack only estimates rows it has not
                // drawn, so when a section above the viewport shrinks or leaves,
                // the viewport can land past the rows it drew and stay blank
                // until a scroll, and it jumps as estimates correct while
                // scrolling. The tail is paged, so every row it holds is exact.
                VStack(alignment: .leading, spacing: 0) {
                    if entries.isEmpty && !hasOlder && !runIsOpen { invitation }
                    if hasOlder || segments.count > visibleSegmentLimit {
                        Button { showEarlier(segments: segments.count, firstVisible: visible.first?.id, proxy: proxy) } label: {
                            HStack { if loadingOlder { ProgressView().controlSize(.small) }; Text(loadingOlder ? "Loading older messages…" : "Load earlier messages") }
                        }.buttonStyle(.plain).font(.system(size: 12 * scale)).frame(maxWidth: .infinity).padding(.bottom, 16).disabled(loadingOlder)
                    }
                    ForEach(visible) { segment in
                        Group {
                            switch segment.content {
                            case .speaker(let item): speaker(item, live: liveSpeakers.contains(segment.id))
                            case .entry(let item): row(item, live: live)
                            case .fold(let items): fold(items, live: live)
                            case .changes(let items): changes(items)
                            }
                        }.padding(.top, gap(segment.spacing)).id(segment.id)
                    }
                    if runIsOpen && activities.isEmpty && live.isEmpty {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small).tint(chat.providerAccent)
                            Text("Thinking…").font(.system(size: textSize)).foregroundStyle(CompanionTheme.secondary)
                        }.padding(.top, 6 * scale)
                    }
                    Color.clear.frame(height: 36 * scale).id(Self.bottomID)
                }
                .frame(maxWidth: 760 * scale).frame(maxWidth: .infinity)
                .padding(.horizontal, 20).padding(.top, 16).padding(.bottom, 4)
                .background(CompanionScrollProbe { placed($0, proxy: proxy) })
            }
            .modifier(CompanionKeyboardDismissal())
            .onAppear { proxy.scrollTo(Self.bottomID, anchor: .bottom) }
            .onChange(of: entries) { old, new in
                if let first = old.first, new.first?.id != first.id, new.contains(where: { $0.id == first.id }) {
                    // Older rows from the Mac arrive above the page the reader
                    // already holds; keep all of it and the row they were on.
                    followsLatest = false
                    visibleSegmentLimit = max(visibleSegmentLimit, outline(live: liveEntryIDs).count)
                    let anchor = olderAnchor ?? first.id
                    olderAnchor = nil
                    DispatchQueue.main.async { proxy.scrollTo(anchor, anchor: .top) }
                } else if followsLatest { proxy.scrollTo(Self.bottomID, anchor: .bottom) }
            }
            .onChange(of: followsLatest) { _, follows in
                // Back at the latest activity, history paged in for reading goes again.
                guard follows, visibleSegmentLimit > Self.page else { return }
                visibleSegmentLimit = Self.page
                DispatchQueue.main.async { proxy.scrollTo(Self.bottomID, anchor: .bottom) }
            }
            .onChange(of: jumpToken) { _, _ in
                guard let jumpEntryID else { return }
                let segments = outline(live: liveEntryIDs)
                guard let index = segments.firstIndex(where: { segment in
                    switch segment.content {
                    case .entry(let item): return item.id == jumpEntryID
                    case .fold(let items), .changes(let items): return items.contains { $0.id == jumpEntryID }
                    case .speaker: return false
                    }
                }) else { return }
                let target = segments[index].id
                if case .fold = segments[index].content { foldChoice[target] = true }
                followsLatest = false
                if segments.count - index > visibleSegmentLimit { visibleSegmentLimit = segments.count - index + 20 }
                // After the page and fold have laid out.
                DispatchQueue.main.async { proxy.scrollTo(target, anchor: .center) }
            }
            .overlay(alignment: .bottomTrailing) {
                if !followsLatest {
                    Button { followsLatest = true; proxy.scrollTo(Self.bottomID, anchor: .bottom) } label: {
                        Label("Latest", systemImage: "arrow.down").font(.system(size: 13 * scale, weight: .medium))
                            .padding(.horizontal, 12).padding(.vertical, 8).background(.regularMaterial, in: Capsule())
                    }.buttonStyle(.plain).padding(12).accessibilityLabel("Go to latest activity")
                }
            }
        }
    }

    /// Following is the reader's choice, as on the Mac: reaching the end
    /// resumes it and scrolling away pauses it, while rows growing or
    /// leaving under a following viewport only bring the end back into view.
    private func placed(_ placement: CompanionScrollPlacement, proxy: ScrollViewProxy) {
        if placement.distanceFromEnd <= 36 * scale + 6 {
            if !followsLatest { followsLatest = true }
        } else if placement.interacting || placement.scrolled {
            if followsLatest { followsLatest = false }
        } else if placement.resized, followsLatest {
            proxy.scrollTo(Self.bottomID, anchor: .bottom)
        }
    }

    /// Page in rows the phone already holds before asking the Mac for more,
    /// keeping the row being read where it is.
    private func showEarlier(segments: Int, firstVisible: String?, proxy: ScrollViewProxy) {
        followsLatest = false
        if segments > visibleSegmentLimit {
            visibleSegmentLimit += Self.page
            if let firstVisible { DispatchQueue.main.async { proxy.scrollTo(firstVisible, anchor: .top) } }
        } else {
            olderAnchor = firstVisible
            loadOlder()
        }
    }

    /// A chat with no turns yet says who it will talk to, as the Mac does.
    private var invitation: some View {
        let folder = (workspace as NSString).lastPathComponent
        let who = teamEnabled ? "the Team" : chat.displayModel
        return VStack(spacing: 6) {
            Text(folder.isEmpty ? "Message \(who)." : "Message \(who) about \(folder).").font(.system(size: textSize))
            Text("Tools run on your Mac inside that folder and ask before changing files, unless the chat allows them.")
                .font(.system(size: 12 * scale)).foregroundStyle(CompanionTheme.secondary)
        }.multilineTextAlignment(.center).frame(maxWidth: 460).frame(maxWidth: .infinity).padding(.top, 60)
    }

    private func presentation(_ item: RemoteTranscriptItem) -> CompanionPresentation {
        let member = members.first { $0["id"].text == item.memberID || $0["name"].text == item.memberName }
        let memberRoute = member?["route"].text ?? ""
        let chosenRoute = String((member?["choice"].text ?? "").split(separator: "|").first ?? "")
        let fallback = !memberRoute.isEmpty ? memberRoute : !chosenRoute.isEmpty ? chosenRoute : chat.route ?? fallbackRoute
        let route = item.route.isEmpty ? fallback : item.route
        return CompanionBranding.presentation(route: route, label: member?["label"].text ?? chat.displayModel, models: models)
    }
    private func speaker(_ item: RemoteTranscriptItem, live: Bool) -> some View {
        let brand = presentation(item)
        return ChatSharedSpeakerHeader(member: item.memberName, label: brand.label, timestamp: ChatSectionTime.label(item.time),
            accent: brand.accent, ink: CompanionTheme.ink, secondary: CompanionTheme.secondary, live: live, scale: scale) {
                CompanionProviderMark(presentation: brand, size: 15 * scale)
            }
    }
    @ViewBuilder private func row(_ item: RemoteTranscriptItem, live: Set<String>) -> some View {
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
                CompanionMentionText(text: entry.text, mentions: entry.mentions, models: models, size: textSize)
                    .padding(.horizontal, 13 * scale).padding(.vertical, 8 * scale)
                    .background(RoundedRectangle(cornerRadius: 15 * scale, style: .continuous).fill(CompanionTheme.selection))
                }
            }.accessibilityLabel("You: " + entry.text)
        case "assistant":
            let reply = ChatReplySources.split(entry.text)
            VStack(alignment: .leading, spacing: 8 * scale) {
                if ChatReplySources.isBlank(reply.body), live.contains(item.id) {
                    ChatSharedShimmerText(text: "Thinking…", active: runIsOpen, accent: brand.accent, base: CompanionTheme.secondary)
                        .font(.system(size: textSize))
                } else { CompanionMessageText(text: reply.body, size: textSize).equatable() }
                if !reply.links.isEmpty {
                    HStack { ForEach(reply.links) { link in if let url = URL(string: link.url) { Link(link.title, destination: url).font(.system(size: 11 * scale)) } } }
                }
            }.frame(maxWidth: .infinity, alignment: .leading)
        case "tool": tool(item, brand: brand, live: live)
        default:
            Text(entry.text).font(.system(size: 11.5 * scale)).foregroundStyle(entry.isError ? .red : CompanionTheme.secondary).textSelection(.enabled)
        }
    }
    private func tool(_ item: RemoteTranscriptItem, brand: CompanionPresentation, live liveIDs: Set<String>) -> some View {
        let live = liveIDs.contains(item.id) && item.detail == "Running…"
        let display = ChatToolDisplay.describe(tool: item.tool, summary: item.summary ?? item.text, live: live)
        return VStack(alignment: .leading, spacing: 6 * scale) {
            Button { withAnimation(.easeInOut(duration: 0.15)) { toggle(item.id) } } label: {
                HStack(spacing: 7 * scale) {
                    ChatToolGlyph(name: ChatToolDisplay.glyph(item.tool), size: 15 * scale).foregroundStyle(brand.accent)
                    ChatSharedShimmerText(text: display.verb, active: live, accent: brand.accent, base: CompanionTheme.ink)
                        .font(.system(size: 12 * scale, weight: .medium)).fixedSize()
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
        var changes = ChatTurnChanges.collect(items)
        let entryIDs = Set(items.map(\.id))
        if let summary = turnSummaries.last(where: { !$0["files"].rows.isEmpty && $0["entryIDs"].rows.contains { entryIDs.contains($0.text) } }) {
            changes.files = summary["files"].rows.map { row in
                ChatTurnChanges.File(path: row["path"].text, members: row["members"].rows.map(\.text), added: row["added"].int, removed: row["removed"].int, diff: row["diff"].text)
            }
        }
        return ChatSharedTurnChanges(changes: changes, scale: scale,
            ink: CompanionTheme.ink, secondary: CompanionTheme.secondary, hairline: CompanionTheme.hairline,
            fill: CompanionTheme.cardFill, rim: CompanionTheme.cardRim) { _ in EmptyView() }
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
    private func fold(_ items: [RemoteTranscriptItem], live liveIDs: Set<String>) -> some View {
        let id = ChatTranscriptSegment<RemoteTranscriptItem>.foldID(items)
        let open = foldChoice[id] ?? items.contains { expanded.contains($0.id) }
        let live = items.contains { liveIDs.contains($0.id) }
        let summary = ChatToolFoldSummary.collect(items)
        return VStack(alignment: .leading, spacing: 4 * scale) {
            Button {
                if reduceMotion { foldChoice[id] = !open }
                else { withAnimation(.easeInOut(duration: 0.15)) { foldChoice[id] = !open } }
            } label: {
                ChatSharedFoldHeader(summary: summary, live: live, open: open,
                    accent: items.first.map(presentation)?.accent ?? chat.providerAccent,
                    ink: CompanionTheme.ink, secondary: CompanionTheme.secondary, scale: scale)
                    .padding(.horizontal, 6).padding(.vertical, 4 * scale)
                    .contentShape(Rectangle()).padding(.horizontal, -6)
            }.buttonStyle(.plain)
                .accessibilityLabel((live ? "Working, " : "Worked, ") + summary.activity)
                .accessibilityValue(open ? "Expanded" : "Collapsed")
            if open {
                VStack(alignment: .leading, spacing: 2 * scale) {
                    ForEach(items, id: \.id) { row($0, live: liveIDs).id($0.id) }
                }.padding(.leading, 18 * scale)
                    .overlay(alignment: .leading) {
                        CompanionTheme.hairline.frame(width: 1).padding(.leading, 7 * scale).padding(.vertical, 2)
                    }
            }
        }
    }
}

/// Native selectable text on iOS, with the desktop's reading size and code cards.
/// Equatable on its inputs, so a streaming update re-parses only the reply
/// that changed rather than every reply on the page.
private struct CompanionMessageText: View, Equatable {
    let text: String
    var size: CGFloat = 13
    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            ForEach(Array(CompanionMarkdown.blocks(text).enumerated()), id: \.offset) { _, block in
                switch block {
                case .code(let language, let source):
                    VStack(alignment: .leading, spacing: 6) {
                        if !language.isEmpty { Text(language).font(.system(size: size * 0.81)).foregroundStyle(CompanionTheme.secondary) }
                        ScrollView(.horizontal) { Text(source).font(.system(size: size, design: .monospaced)).textSelection(.enabled) }
                    }.padding(12).companionCard(cornerRadius: 8)
                case .heading(let level, let source):
                    inline(source, size: headingSize(level)).font(.system(size: headingSize(level), weight: .semibold))
                        .frame(maxWidth: .infinity, alignment: .leading)
                case .paragraph(let source):
                    inline(source, size: size).font(.system(size: size)).frame(maxWidth: .infinity, alignment: .leading)
                case .table(let headers, let rows):
                    ScrollView(.horizontal, showsIndicators: false) {
                        VStack(alignment: .leading, spacing: 0) {
                            tableRow(headers, header: true)
                            ForEach(Array(rows.enumerated()), id: \.offset) { _, row in tableRow(row, header: false) }
                        }.fixedSize(horizontal: true, vertical: false)
                    }.companionCard(cornerRadius: 8)
                }
            }
        }
    }

    /// Selectable, with a swatch after each colour code; fenced code above
    /// stays plain.
    private func inline(_ source: String, size: CGFloat) -> some View {
        ChatSharedHexText(text: (try? AttributedString(markdown: source, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace)))
            ?? AttributedString(source), size: size)
    }

    private func headingSize(_ level: Int) -> CGFloat {
        size * ([1: 1.55, 2: 1.38, 3: 1.24, 4: 1.14, 5: 1.07, 6: 1.0][level] ?? 1.0)
    }

    private func tableRow(_ cells: [String], header: Bool) -> some View {
        HStack(alignment: .top, spacing: 0) {
            ForEach(Array(cells.enumerated()), id: \.offset) { _, cell in
                inline(cell, size: size).font(.system(size: size, weight: header ? .semibold : .regular))
                    .frame(minWidth: 84, maxWidth: 220, alignment: .leading)
                    .padding(.horizontal, 9).padding(.vertical, 7)
                    .overlay(alignment: .trailing) { CompanionTheme.hairline.frame(width: 0.5) }
            }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .background(header ? CompanionTheme.hairline.opacity(0.22) : Color.clear)
        .overlay(alignment: .bottom) { CompanionTheme.hairline.frame(height: 0.5) }
    }
}

/// Small, bounded block grammar for the companion. Inline emphasis and links
/// use Foundation's Markdown parser; headings, tables and fenced code get
/// native SwiftUI structure without a WebView or third-party dependency.
/// Fences and table rows follow the desktop's ChatTableParser rules.
enum CompanionMarkdown {
    enum Block: Equatable {
        case paragraph(String)
        case heading(Int, String)
        case table([String], [[String]])
        case code(String, String)
    }

    static func blocks(_ source: String) -> [Block] {
        guard source.utf8.count <= 256 * 1024 else { return [.paragraph(source)] }
        let lines = source.components(separatedBy: "\n").map { $0.hasSuffix("\r") ? String($0.dropLast()) : $0 }
        var result: [Block] = [], prose: [String] = [], index = 0, tableCount = 0
        func flush() {
            let value = prose.joined(separator: "\n").trimmingCharacters(in: .whitespacesAndNewlines)
            if !value.isEmpty { result.append(.paragraph(value)) }
            prose.removeAll(keepingCapacity: true)
        }
        while index < lines.count {
            let line = lines[index]
            // A backtick info string cannot hold backticks: ```x``` is inline code.
            if let open = fence(line), open.marker == "~" || !open.info.contains("`") {
                flush()
                let indent = line.prefix(while: { $0 == " " }).count
                let language = String(open.info.split(whereSeparator: { $0 == " " || $0 == "\t" }).first ?? "")
                index += 1
                var code: [String] = [], closed = false
                while index < lines.count {
                    let body = lines[index]
                    index += 1
                    // Only the opener's character, at least as many times and
                    // with nothing after it, closes the block; anything shorter
                    // or followed by text is part of the code.
                    if let end = fence(body), end.marker == open.marker, end.count >= open.count, end.info.isEmpty {
                        closed = true; break
                    }
                    code.append(String(body.dropFirst(min(indent, body.prefix(while: { $0 == " " }).count))))
                }
                // An unfinished block keeps everything after its opener except
                // the empty line a trailing newline leaves.
                if !closed, code.last?.isEmpty == true { code.removeLast() }
                result.append(.code(language, code.joined(separator: "\n")))
                continue
            }
            if let (level, heading) = heading(line) {
                flush(); result.append(.heading(level, heading)); index += 1; continue
            }
            if tableCount < 16, index + 1 < lines.count,
               let header = cells(lines[index]), let separators = cells(lines[index + 1]),
               header.count <= 12, header.count == separators.count, separators.allSatisfy(isDelimiter) {
                flush(); index += 2
                var rows: [[String]] = []
                // A row wider than the header ends the table, as on the desktop,
                // so its text stays visible instead of losing the extra cells.
                while index < lines.count, rows.count < 128, let row = cells(lines[index]), row.count <= header.count {
                    rows.append(row + Array(repeating: "", count: header.count - row.count))
                    index += 1
                }
                result.append(.table(header, rows)); tableCount += 1; continue
            }
            if line.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty { flush() }
            else { prose.append(line) }
            index += 1
        }
        flush()
        return result.isEmpty ? [.paragraph(source)] : result
    }

    private static func heading(_ line: String) -> (Int, String)? {
        let count = line.prefix(while: { $0 == "#" }).count
        guard (1...6).contains(count), line.dropFirst(count).first == " " else { return nil }
        return (count, String(line.dropFirst(count)).trimmingCharacters(in: .whitespaces))
    }

    /// Four-space indentation is code, and quoted lines are outside this small
    /// grammar.
    private static func unindented(_ line: String) -> Substring? {
        let spaces = line.prefix(while: { $0 == " " }).count
        guard spaces <= 3, !line.hasPrefix("\t") else { return nil }
        let value = line.dropFirst(spaces)
        guard !value.hasPrefix(">"), !value.hasPrefix("\t") else { return nil }
        return value
    }

    private static func fence(_ line: String) -> (marker: Character, count: Int, info: String)? {
        guard let value = unindented(line), let first = value.first, first == "`" || first == "~" else { return nil }
        let count = value.prefix(while: { $0 == first }).count
        guard count >= 3 else { return nil }
        return (first, count, value.dropFirst(count).trimmingCharacters(in: .whitespaces))
    }

    /// Unescaped pipes outside matching backtick runs split columns. An
    /// escaped pipe is cell text, inside code spans too, so `a\|b` is one cell
    /// reading a|b; other escapes stay for the inline renderer.
    private static func cells(_ line: String) -> [String]? {
        guard let value = unindented(line) else { return nil }
        let trimmed = value.trimmingCharacters(in: .whitespaces)
        let characters = Array(trimmed)
        var parts: [String] = [], part = "", codeRun: Int?
        var index = 0, pipe = false, lastWasPipe = false
        while index < characters.count {
            let character = characters[index]
            if character == "\\", index + 1 < characters.count {
                if characters[index + 1] != "|" { part.append(character) }
                part.append(characters[index + 1]); index += 2; lastWasPipe = false
            } else if character == "`" {
                var end = index + 1
                while end < characters.count, characters[end] == "`" { end += 1 }
                let count = end - index
                if codeRun == count { codeRun = nil } else if codeRun == nil { codeRun = count }
                part += String(repeating: "`", count: count); index = end; lastWasPipe = false
            } else if character == "|", codeRun == nil {
                parts.append(part.trimmingCharacters(in: .whitespaces)); part = ""
                pipe = true; lastWasPipe = true; index += 1
            } else {
                part.append(character); index += 1; lastWasPipe = false
            }
        }
        guard pipe, codeRun == nil else { return nil }
        parts.append(part.trimmingCharacters(in: .whitespaces))
        if trimmed.hasPrefix("|") { parts.removeFirst() }
        if lastWasPipe { parts.removeLast() }
        return parts.isEmpty ? nil : parts
    }

    private static func isDelimiter(_ cell: String) -> Bool {
        let clean = cell.replacingOccurrences(of: ":", with: "")
        return clean.count >= 3 && clean.allSatisfy { $0 == "-" }
    }
}

/// Reading the thread puts the keyboard away: dragging it down, as in
/// Messages, or tapping it. Taps still reach the rows' own controls.
struct CompanionKeyboardDismissal: ViewModifier {
    func body(content: Content) -> some View {
        #if os(iOS)
        content.scrollDismissesKeyboard(.interactively)
            .simultaneousGesture(TapGesture().onEnded {
                UIApplication.shared.sendAction(#selector(UIResponder.resignFirstResponder), to: nil, from: nil, for: nil)
            })
        #else
        content
        #endif
    }
}

/// Where the transcript's viewport sits, and what moved it since the last report.
struct CompanionScrollPlacement: Equatable {
    /// How far the content runs below the viewport's bottom edge.
    var distanceFromEnd: CGFloat
    /// The viewport moved over unchanged content: a scroll, not growth.
    var scrolled: Bool
    /// The content or the viewport changed size.
    var resized: Bool
    /// A finger is on the content, or it is still gliding from one.
    var interacting: Bool
}

#if os(iOS)
/// Observes the transcript's own UIScrollView. SwiftUI reports no scroll
/// position before iOS 18, and preferences from inside the scroll view do not
/// reach it on current releases, so the viewport is read where it lives. The
/// probe never takes touches and adds no scroll view of its own.
struct CompanionScrollProbe: UIViewRepresentable {
    let report: (CompanionScrollPlacement) -> Void
    func makeUIView(context: Context) -> Probe { Probe() }
    func updateUIView(_ view: Probe, context: Context) { view.report = report; view.attach() }
    static func dismantleUIView(_ view: Probe, coordinator: ()) { view.detach() }

    final class Probe: UIView {
        var report: (CompanionScrollPlacement) -> Void = { _ in }
        private weak var scroll: UIScrollView?
        private var observations: [NSKeyValueObservation] = []
        private var last: (offset: CGFloat, content: CGFloat, viewport: CGFloat)?
        private var queued = false

        override func hitTest(_ point: CGPoint, with event: UIEvent?) -> UIView? { nil }
        override func didMoveToWindow() {
            super.didMoveToWindow()
            if window == nil { detach() } else { attach() }
        }

        func attach() {
            guard window != nil else { return }
            var view = superview
            while let current = view, !(current is UIScrollView) { view = current.superview }
            guard let found = view as? UIScrollView, found !== scroll else { return }
            detach(); scroll = found
            let changed: (UIScrollView) -> Void = { [weak self] _ in self?.queue() }
            observations = [found.observe(\.contentOffset) { scroll, _ in changed(scroll) },
                            found.observe(\.contentSize) { scroll, _ in changed(scroll) },
                            found.observe(\.bounds) { scroll, _ in changed(scroll) }]
            queue()
        }

        func detach() { observations.removeAll(); scroll = nil; last = nil }

        /// Offsets change inside UIKit and SwiftUI layout; report once, after it.
        private func queue() {
            guard !queued else { return }
            queued = true
            DispatchQueue.main.async { [weak self] in
                guard let self else { return }
                self.queued = false
                self.measure()
            }
        }

        private func measure() {
            guard let scroll else { return }
            let offset = scroll.contentOffset.y, content = scroll.contentSize.height, viewport = scroll.bounds.height
            let distance = content + scroll.adjustedContentInset.bottom - (offset + viewport)
            let previous = last
            last = (offset, content, viewport)
            let resized = previous.map { abs($0.content - content) > 0.5 || abs($0.viewport - viewport) > 0.5 } ?? true
            let scrolled = !resized && previous.map { abs($0.offset - offset) > 0.5 } ?? false
            report(CompanionScrollPlacement(distanceFromEnd: distance, scrolled: scrolled, resized: resized,
                                            interacting: scroll.isTracking || scroll.isDragging || scroll.isDecelerating))
        }
    }
}
#else
/// The package also builds for macOS tests, where the Mac app's own
/// transcript follows its viewport; nothing is observed here.
struct CompanionScrollProbe: View {
    let report: (CompanionScrollPlacement) -> Void
    var body: some View { Color.clear }
}
#endif
