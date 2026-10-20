import AppKit
import SwiftUI

// Transcript rows shared by the Chat window and the inspector's helper and
// Side Chat transcripts. ChatTranscriptLayout decides what is drawn; these
// views decide how. Every size comes from ChatTextStyle, so the reading zoom
// scales the transcript and nothing else. Colour stays semantic or the
// speaker's provider accent: a tool glyph wears the accent of the model (or
// Team member) that called it.

private typealias Semantic = HubTheme.Semantic

extension ChatEntry: ChatTranscriptItem {}

/// The recorded patch of a local patch row, or nothing for every other tool;
/// shell output that happens to look like a diff never counts as one.
func chatPatchText(_ entry: ChatEntry) -> String {
    guard entry.tool == "apply_patch" else { return "" }
    return entry.detail?.isEmpty == false ? entry.detail! : entry.text
}

func chatReveal(_ path: String, in workspace: String?) {
    let full = path.hasPrefix("/") ? path : ((workspace ?? NSHomeDirectory()) as NSString).appendingPathComponent(path)
    NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: full)])
}

/// A speaker's mark and name, once above a run of their replies and tool
/// calls. A Team member leads with their own name, then the model.
struct ChatSpeakerHeader: View {
    var member: String?
    var time: Double? = nil
    var label: String
    var presentation: ProviderPresentation?
    var accent: Color
    var live = false
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        ChatSharedSpeakerHeader(member: member, label: label, timestamp: ChatSectionTime.label(time),
            accent: accent, ink: Semantic.ink, secondary: Semantic.secondaryInk, live: live, scale: textStyle.scaled(1)) {
                ChatProviderIcon(presentation: presentation, size: textStyle.scaled(15))
        }
        .help(ChatSectionTime.help(time) ?? label)
    }
}

/// Reactions attached to a message, grouped by emoji and naming the first
/// reactor ("Dave + 2 more 👍"); hovering names everyone. With `onReact`,
/// clicking a capsule adds the user's reaction with that emoji or withdraws it.
struct ChatReactionsRow: View {
    var reactions: [ChatReaction]
    /// (emoji, the user already reacted with it) -> add or withdraw.
    var onReact: ((String, Bool) -> Void)? = nil
    @Environment(\.chatTextStyle) private var textStyle

    private var clusters: [ChatReactionCluster] {
        ChatReactionCluster.group(reactions.map { (emoji: $0.emoji, author: $0.author, name: $0.authorName) })
    }

    var body: some View {
        ViewThatFits(in: .horizontal) {
            HStack(spacing: textStyle.scaled(4)) { chips }
                .fixedSize(horizontal: true, vertical: false)
            VStack(alignment: .leading, spacing: textStyle.scaled(4)) { chips }
        }
    }

    @ViewBuilder private var chips: some View {
        ForEach(clusters) { cluster in
            if let onReact {
                Button { onReact(cluster.emoji, cluster.mine) } label: { capsule(cluster) }
                    .buttonStyle(.plain)
                    .help(cluster.caption)
                    .accessibilityLabel(cluster.caption)
            } else {
                capsule(cluster).help(cluster.caption).accessibilityLabel(cluster.caption)
            }
        }
    }

    private func capsule(_ cluster: ChatReactionCluster) -> some View {
        HStack(spacing: textStyle.scaled(4)) {
            Text(cluster.label).font(textStyle.system(10.5, weight: .semibold))
                .foregroundStyle(Semantic.secondaryInk).lineLimit(1)
            Text(cluster.emoji).font(textStyle.system(12))
        }
        .padding(.horizontal, textStyle.scaled(7)).padding(.vertical, textStyle.scaled(3))
        .background(Capsule().fill(Semantic.raisedSurface))
        .overlay(Capsule().stroke(cluster.mine ? Semantic.ink.opacity(0.35) : Semantic.hairline, lineWidth: 1))
        .contentShape(Capsule())
    }
}

/// A stable action gutter beside message text. Hovering reveals the button
/// without moving the text or covering selection, links, or reaction chips.
private struct ChatMessageCopy: ViewModifier {
    var text: String
    var leading = false
    @State private var hovering = false
    @State private var copied = false
    @FocusState private var focused: Bool
    @Environment(\.chatTextStyle) private var textStyle

    func body(content: Content) -> some View {
        if text.isEmpty {
            content
        } else {
            content
                .padding(leading ? .leading : .trailing, textStyle.scaled(30))
                .overlay(alignment: leading ? .topLeading : .topTrailing) {
                    Button {
                        ChatTranscriptTable.copy(text)
                        copied = true
                    } label: {
                        Image(systemName: copied ? "checkmark" : "doc.on.doc")
                            .font(textStyle.system(11, weight: .medium))
                            .foregroundStyle(Semantic.secondaryInk)
                            .frame(width: textStyle.scaled(24), height: textStyle.scaled(26))
                            .background(RoundedRectangle(cornerRadius: textStyle.scaled(5)).fill(Semantic.surface))
                            .contentShape(Rectangle())
                    }
                    .buttonStyle(.plain)
                    .focused($focused)
                    .opacity(hovering || focused || copied ? 1 : 0)
                    .allowsHitTesting(hovering || focused || copied)
                    .help(copied ? "Copied message" : "Copy message")
                    .accessibilityLabel(copied ? "Message copied" : "Copy message")
                }
                .contentShape(Rectangle())
                .onHover { value in
                    hovering = value
                    if !value { copied = false }
                }
                .onChange(of: text) { _, _ in copied = false }
                .onChange(of: focused) { _, value in
                    if !value && !hovering { copied = false }
                }
        }
    }
}

/// The user's turn, as a bubble on the trailing edge, opening with a bold
/// **User:** label. In a Team transcript members' replies and tool calls
/// interleave with the user's messages, and an instruction has been read as
/// the preceding member's words; the label names the speaker inside the
/// bubble itself. In a Team its recorded `@Name` tags wear their members'
/// accents, as they did in the composer.
struct UserRow: View {
    var entry: ChatEntry
    var title = "You"
    /// A route's provider accent; without one, tags read as plain text.
    var accent: ((String) -> Color)? = nil
    /// The user's reaction toggle; nil shows reactions without one.
    var onReact: ((String, Bool) -> Void)? = nil
    @Environment(\.chatTextStyle) private var textStyle

    /// The bubble's text: the speaker label in bold, then the message with its
    /// mention tags. A message with only attachments still carries the label.
    static func labelled(_ entry: ChatEntry, title: String, accent: ((String) -> Color)?) -> AttributedString {
        var label = AttributedString(title == "You" ? "User:" : title + ":")
        label.inlinePresentationIntent = .stronglyEmphasized
        guard !entry.text.isEmpty else { return label }
        let message = accent.map { ChatMentions.marked(entry.text, mentions: entry.mentions ?? [], accent: $0) }
            ?? AttributedString(entry.text)
        return label + AttributedString(" ") + message
    }

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            if title != "You" {
                Text(title).font(textStyle.system(10.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
            }
            VStack(alignment: .leading, spacing: textStyle.scaled(6)) {
                if let attachments = entry.attachments, !attachments.isEmpty { ChatAttachmentStrip(attachments: attachments) }
                ChatSelectableText(text: Self.labelled(entry, title: title, accent: accent),
                                   font: textStyle.nsFont, color: Semantic.nsInk, message: entry.text.isEmpty ? nil : entry.text)
            }
            .padding(.horizontal, textStyle.scaled(13)).padding(.vertical, textStyle.scaled(8))
            .background(RoundedRectangle(cornerRadius: textStyle.scaled(15), style: .continuous).fill(Semantic.selection))
            if let reactions = entry.reactions, !reactions.isEmpty {
                // Tapbacks hang off the bubble's bottom trailing corner.
                ChatReactionsRow(reactions: reactions, onReact: onReact)
                    .padding(.top, -textStyle.scaled(8)).padding(.trailing, textStyle.scaled(10))
            }
        }
        .modifier(ChatMessageCopy(text: entry.text, leading: true))
        .frame(maxWidth: .infinity, alignment: .trailing)
        .padding(.leading, 56)
        .accessibilityElement(children: .contain)
        .accessibilityLabel(title)
    }
}

/// A reply's text, its live "Thinking…" state, and any source links the
/// runtime appended. The speaker header is drawn separately. The text is
/// natively selectable; its right-click menu adds **Copy Message**, which
/// copies the reply as saved, sources line included.
struct AssistantRow: View {
    var entry: ChatEntry
    var accent: Color
    var streaming: Bool
    /// The user's reaction toggle; nil shows reactions without one.
    var onReact: ((String, Bool) -> Void)? = nil
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        let reply = ChatReplySources.split(entry.text)
        VStack(alignment: .leading, spacing: textStyle.scaled(8)) {
            if ChatReplySources.isBlank(reply.body) {
                if streaming { ChatShimmerText(text: "Thinking…", active: true, accent: accent).font(textStyle.font) }
            } else {
                ChatTranscriptText(text: reply.body, streaming: streaming, message: entry.text)
            }
            if !reply.links.isEmpty { ChatSourcesRow(links: reply.links) }
            // A quiet chip row under the reply; replies stay sections, never bubbles.
            if let reactions = entry.reactions, !reactions.isEmpty { ChatReactionsRow(reactions: reactions, onReact: onReact) }
        }
        .frame(maxWidth: .infinity, alignment: .leading)
        .modifier(ChatMessageCopy(text: entry.text))
        .accessibilityElement(children: .contain)
    }
}

/// Search citations as one quiet pill ("12 sources · github.com · …") that
/// opens into a numbered list, instead of a paragraph of links.
struct ChatSourcesRow: View {
    var links: [ChatReplySources.Link]
    @State private var open = false
    @Environment(\.chatTextStyle) private var textStyle
    @Environment(\.accessibilityReduceMotion) private var reduceMotion

    var body: some View {
        VStack(alignment: .leading, spacing: textStyle.scaled(6)) {
            Button {
                if reduceMotion { open.toggle() } else { withAnimation(.easeInOut(duration: 0.15)) { open.toggle() } }
            } label: {
                HStack(spacing: textStyle.scaled(6)) {
                    ChatToolGlyph(name: "web_search", size: textStyle.scaled(14)).foregroundStyle(Semantic.secondaryInk)
                    Text(links.count == 1 ? "1 source" : "\(links.count) sources")
                        .font(textStyle.system(11.5, weight: .medium)).foregroundStyle(Semantic.ink)
                    Text(hosts).font(textStyle.system(11.5)).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
                    Image(systemName: "chevron.right").font(textStyle.system(8.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                        .rotationEffect(.degrees(open ? 90 : 0))
                }
                .padding(.horizontal, textStyle.scaled(10)).padding(.vertical, textStyle.scaled(5))
                .background(Capsule().fill(Semantic.raisedSurface))
                .overlay(Capsule().stroke(Semantic.hairline, lineWidth: 1))
                .contentShape(Capsule())
            }
            .buttonStyle(.plain)
            .accessibilityLabel(links.count == 1 ? "1 source" : "\(links.count) sources")
            .accessibilityValue(open ? "Expanded" : "Collapsed")
            if open {
                VStack(alignment: .leading, spacing: textStyle.scaled(4)) {
                    ForEach(Array(links.enumerated()), id: \.offset) { index, link in
                        if let url = URL(string: link.url) {
                            Link(destination: url) {
                                HStack(spacing: textStyle.scaled(7)) {
                                    Text("\(index + 1)").font(textStyle.system(10.5, design: .monospaced))
                                        .foregroundStyle(Semantic.secondaryInk).frame(minWidth: textStyle.scaled(14), alignment: .trailing)
                                    Text(link.title).font(textStyle.system(12)).foregroundStyle(Semantic.ink).lineLimit(1)
                                    Text(link.host).font(textStyle.system(11)).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
                                }
                            }
                            .buttonStyle(.plain).help(link.url)
                            .accessibilityLabel(link.title + ", " + link.host)
                        }
                    }
                }
                .padding(.leading, textStyle.scaled(4))
            }
        }
    }

    private var hosts: String {
        var seen: [String] = []
        for link in links where !seen.contains(link.host) { seen.append(link.host) }
        return seen.prefix(3).joined(separator: " · ") + (seen.count > 3 ? " · …" : "")
    }
}

/// One tool call: the caller's glyph in their accent, a past-tense verb and
/// what it acted on ("Ran swift build", "Edited Model.swift"). Hovering lifts
/// the row; clicking opens the recorded output beneath it.
struct ToolRow: View {
    var entry: ChatEntry
    var accent: Color
    @Binding var expanded: Bool
    var workspace: String?
    var onInspect: (() -> Void)? = nil
    /// A delegated helper is still working.
    var running = false
    /// The call itself has not recorded a result yet.
    var live = false
    @State private var hovering = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.chatTextStyle) private var textStyle

    private var display: ChatToolDisplay { .describe(tool: entry.tool, summary: entry.summary ?? entry.text, live: live) }

    var body: some View {
        VStack(alignment: .leading, spacing: textStyle.scaled(6)) {
            HStack(spacing: 8) {
                header
                if let onInspect {
                    Button(action: onInspect) { Image(systemName: "arrow.up.right").font(.system(size: 10)).frame(width: 20, height: 22) }
                        .buttonStyle(.plain).foregroundStyle(.secondary).help("Inspect subagent transcript")
                        .accessibilityLabel("Inspect subagent transcript")
                }
            }
            if expanded { details }
        }
    }

    private var header: some View {
        let display = display
        return Button {
            if reduceMotion { expanded.toggle() } else { withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() } }
        } label: {
            HStack(spacing: textStyle.scaled(7)) {
                ChatToolGlyph(name: ChatToolDisplay.glyph(entry.tool), size: textStyle.scaled(15)).foregroundStyle(accent)
                ChatShimmerText(text: display.verb, active: live, accent: accent, base: Semantic.ink)
                    .font(textStyle.system(12, weight: .medium)).fixedSize()
                if !display.subject.isEmpty {
                    Text(display.subject)
                        .font(display.code ? textStyle.system(11.5, design: .monospaced) : textStyle.system(12))
                        .foregroundStyle(Semantic.secondaryInk).lineLimit(1).truncationMode(.middle)
                }
                if !display.context.isEmpty {
                    Text(display.context).font(textStyle.system(11)).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.middle)
                }
                Spacer(minLength: 0)
                if running { ProgressView().controlSize(.mini).accessibilityHidden(true) }
                if let stats = ChatPatchStats.count(chatPatchText(entry)) { ChatPatchStatsLabel(stats: stats, size: textStyle.scaled(11)) }
                if !entry.changedFiles.isEmpty {
                    Text(entry.changedFiles.count == 1 ? "1 file" : "\(entry.changedFiles.count) files")
                        .font(textStyle.system(11)).foregroundStyle(Semantic.secondaryInk)
                }
                if entry.isError {
                    Image(systemName: "exclamationmark.triangle.fill").font(textStyle.system(10)).foregroundStyle(Color(nsColor: .systemRed))
                }
                Image(systemName: "chevron.right").font(textStyle.system(8.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                    .rotationEffect(.degrees(expanded ? 90 : 0)).opacity(hovering || expanded ? 1 : 0.4)
            }
            .padding(.horizontal, 6).padding(.vertical, textStyle.scaled(4))
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(hovering || expanded ? Semantic.raisedSurface : .clear))
            .padding(.horizontal, -6)
            .contentShape(Rectangle())
        }
        .buttonStyle(.plain)
        .onHover { hovering = $0 }
        .help(String(((entry.tool ?? "tool") + (entry.summary.map { " · " + $0 } ?? "")).prefix(400)))
        .accessibilityLabel(display.verb + (display.subject.isEmpty ? "" : " " + display.subject))
        .accessibilityHint("Tool " + (entry.tool ?? ""))
        .accessibilityValue((expanded ? "Expanded" : "Collapsed") + (running || live ? ", Working" : "") + (entry.isError ? ", Failed" : ""))
    }

    @ViewBuilder private var details: some View {
        let detail = entry.detail?.isEmpty == false ? entry.detail! : entry.text
        let command = display.command
        VStack(alignment: .leading, spacing: textStyle.scaled(6)) {
            if let attachments = entry.attachments, !attachments.isEmpty { ChatAttachmentStrip(attachments: attachments) }
            if entry.tool == "todo_write", let items = ChatTodoChecklist.parse(detail) {
                ChatSharedTodoChecklist(items: items, scale: textStyle.scaled(1), accent: accent, ink: Semantic.ink, secondary: Semantic.secondaryInk)
                    .padding(10).frame(maxWidth: .infinity, alignment: .leading).hubCard(cornerRadius: 8)
            } else if !detail.isEmpty || command != nil {
                VStack(alignment: .leading, spacing: 0) {
                    if let command {
                        Text("$ " + command).font(textStyle.system(11.5, design: .monospaced)).foregroundStyle(Semantic.secondaryInk)
                            .textSelection(.enabled).fixedSize(horizontal: false, vertical: true)
                            .frame(maxWidth: .infinity, alignment: .leading)
                            .padding(.horizontal, 10).padding(.vertical, 7)
                        if !detail.isEmpty { Rectangle().fill(Semantic.hairline).frame(height: 1) }
                    }
                    if !detail.isEmpty {
                        ScrollView([.vertical, .horizontal]) {
                            PatchText(text: detail, size: textStyle.scaled(11.5)).padding(10)
                        }
                        .frame(maxHeight: textStyle.scaled(260))
                    }
                }
                .hubCard(cornerRadius: 8)
            }
            if !entry.changedFiles.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    ForEach(entry.changedFiles, id: \.self) { path in
                        HStack(spacing: 6) {
                            Image(systemName: "doc").font(textStyle.system(10)).foregroundStyle(Semantic.secondaryInk)
                            Text(path).font(textStyle.system(11, design: .monospaced)).foregroundStyle(Semantic.ink)
                                .lineLimit(1).truncationMode(.middle).textSelection(.enabled)
                            Spacer(minLength: 0)
                            Button("Reveal") { chatReveal(path, in: entry.workspace ?? workspace) }
                                .buttonStyle(.plain).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                                .accessibilityLabel("Reveal \(path) in Finder")
                        }
                    }
                }
            }
        }
        .padding(.leading, textStyle.scaled(22))
    }
}

/// The header of a folded run of tool calls: the kinds of work as glyphs in
/// the speaker's accent, then "Worked · 4 commands, 2 edits". While the run
/// is live and closed, the latest step rides along so nothing goes dark.
struct ChatFoldHeader: View {
    var entries: [ChatEntry]
    var live: Bool
    var open: Bool
    var accent: Color
    @State private var hovering = false
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        ChatSharedFoldHeader(summary: .collect(entries), live: live, open: open, accent: accent,
            ink: Semantic.ink, secondary: Semantic.secondaryInk, scale: textStyle.scaled(1), hovering: hovering)
        .padding(.horizontal, 6).padding(.vertical, textStyle.scaled(4))
        .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(hovering ? Semantic.raisedSurface : .clear))
        .padding(.horizontal, -6)
        .contentShape(Rectangle())
        .onHover { hovering = $0 }
    }
}

/// Monospaced output; when it reads as a patch, added and removed lines are
/// tinted and headers recede. One attributed Text keeps selection and copy plain.
struct PatchText: View {
    var text: String
    var onDark = false
    var size: CGFloat = 11.5
    var body: some View {
        ChatSharedPatchText(text: text, onDark: onDark, size: size, ink: Semantic.ink, secondaryInk: Semantic.secondaryInk)
    }
}

/// A parked member's warning uses one line; attempt diagnostics are optional.
struct MemberFailureRow: View {
    var entry: ChatEntry
    @State private var expanded = false
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        VStack(alignment: .leading, spacing: textStyle.scaled(6)) {
            Button { expanded.toggle() } label: {
                HStack(spacing: textStyle.scaled(6)) {
                    Image(systemName: "exclamationmark.circle").font(textStyle.system(11))
                    Text(ChatNotice.failureLine(entry.text, member: entry.memberName))
                        .font(textStyle.system(11)).lineLimit(1).truncationMode(.tail)
                        .layoutPriority(1)
                    Image(systemName: expanded ? "chevron.down" : "chevron.right")
                        .font(textStyle.system(8.5, weight: .semibold))
                    Rectangle().fill(Semantic.hairline).frame(height: 1).frame(minWidth: 12)
                }.foregroundStyle(Semantic.contextCritical)
                    .frame(maxWidth: .infinity, alignment: .leading).contentShape(Rectangle())
            }.buttonStyle(.plain)
                .help(ChatNotice.failureLine(entry.text, member: entry.memberName))
                .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            if expanded {
                Text(entry.detail ?? entry.text).font(textStyle.system(10.5, design: .monospaced))
                    .foregroundStyle(Semantic.secondaryInk).textSelection(.enabled)
                    .fixedSize(horizontal: false, vertical: true).padding(.leading, textStyle.scaled(17))
            }
        }
    }
}

/// A notice as a quiet divider. A Team checkpoint reads as a handoff that
/// names the member who paused; the runtime's full wording stays in the tooltip.
struct NoticeRow: View {
    var entry: ChatEntry
    var member: String? = nil
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        if ChatNotice.style(entry.text) == .failure {
            MemberFailureRow(entry: entry)
        } else {
            divider
        }
    }

    private var divider: some View {
        let checkpoint = ChatNotice.style(entry.text) == .checkpoint
        return HStack(spacing: textStyle.scaled(10)) {
            rule
            HStack(spacing: textStyle.scaled(6)) {
                if checkpoint { ChatToolGlyph(name: "handoff", size: textStyle.scaled(14)) }
                Text(checkpoint ? ChatNotice.checkpointLine(entry.text, member: member) : entry.text)
                    .multilineTextAlignment(.center).fixedSize(horizontal: false, vertical: true)
            }
            .font(textStyle.system(11)).foregroundStyle(Semantic.secondaryInk)
            .layoutPriority(1)
            rule
        }
        .help(entry.text)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(entry.text)
    }

    private var rule: some View {
        Rectangle().fill(Semantic.hairline).frame(height: 1).frame(minWidth: 12, maxWidth: .infinity)
    }
}

/// The close of a Team's collective turn: one quiet table of the files its
/// members changed, with line counts and who touched each. A row opens its
/// diff; a long list shows the first few and offers the rest.
struct ChatTurnChangesRow: View {
    var entries: [ChatEntry]
    var workspace: String?
    var expanded: (String) -> Binding<Bool>
    @State private var showAll = false
    @Environment(\.chatTextStyle) private var textStyle
    private static let preview = 6

    var body: some View {
        ChatSharedTurnChanges(changes: ChatTurnChanges.collect(entries), scale: textStyle.scaled(1),
            ink: Semantic.ink, secondary: Semantic.secondaryInk, hairline: Semantic.hairline,
            fill: Semantic.cardFill, rim: Semantic.cardRim) { path in
                Button("Reveal in Finder") { chatReveal(path, in: workspace) }
                    .buttonStyle(.plain).font(HubTheme.Typography.detail).foregroundStyle(.secondary)
            }
    }
}

private struct ChatTurnChangeFile: View {
    var file: ChatTurnChanges.File
    var workspace: String?
    @Binding var expanded: Bool
    @State private var hovering = false
    @Environment(\.accessibilityReduceMotion) private var reduceMotion
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        let name = (file.path as NSString).lastPathComponent
        let folder = (file.path as NSString).deletingLastPathComponent
        VStack(alignment: .leading, spacing: textStyle.scaled(6)) {
            Button {
                if reduceMotion { expanded.toggle() } else { withAnimation(.easeInOut(duration: 0.15)) { expanded.toggle() } }
            } label: {
                HStack(spacing: textStyle.scaled(6)) {
                    Text(name).font(textStyle.system(11.5, weight: .medium, design: .monospaced)).foregroundStyle(Semantic.ink)
                        .lineLimit(1).layoutPriority(1)
                    if !folder.isEmpty {
                        Text(folder).font(textStyle.system(11, design: .monospaced)).foregroundStyle(.tertiary)
                            .lineLimit(1).truncationMode(.head)
                    }
                    Spacer(minLength: 6)
                    Text(file.members.joined(separator: ", ")).font(textStyle.system(11)).foregroundStyle(Semantic.secondaryInk)
                        .lineLimit(1)
                    if file.added + file.removed > 0 {
                        ChatPatchStatsLabel(stats: ChatPatchStats(added: file.added, removed: file.removed), size: textStyle.scaled(11))
                    }
                    Image(systemName: "chevron.right").font(textStyle.system(8.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                        .rotationEffect(.degrees(expanded ? 90 : 0)).opacity(hovering || expanded ? 1 : 0.4)
                }
                .padding(.horizontal, 10).padding(.vertical, textStyle.scaled(5))
                .background(hovering ? Semantic.raisedSurface : .clear)
                .contentShape(Rectangle())
            }
            .buttonStyle(.plain)
            .onHover { hovering = $0 }
            .help(file.path)
            .accessibilityLabel(file.path + ", changed by " + file.members.joined(separator: ", "))
            .accessibilityValue(expanded ? "Expanded" : "Collapsed")
            if expanded {
                VStack(alignment: .leading, spacing: textStyle.scaled(6)) {
                    if file.diff.isEmpty {
                        Text("No textual diff was recorded for this file.").font(textStyle.system(11.5)).foregroundStyle(Semantic.secondaryInk)
                    } else {
                        ScrollView(.horizontal, showsIndicators: false) { PatchText(text: file.diff, size: textStyle.scaled(11)) }
                    }
                    Button("Reveal in Finder") { chatReveal(file.path, in: workspace) }
                        .buttonStyle(.plain).font(HubTheme.Typography.detail).foregroundStyle(Semantic.secondaryInk)
                }
                .padding(.horizontal, 10).padding(.bottom, textStyle.scaled(8))
            }
        }
    }
}
