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
    var label: String
    var presentation: ProviderPresentation?
    var accent: Color
    var live = false
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        HStack(spacing: textStyle.scaled(7)) {
            ChatProviderIcon(presentation: presentation, size: textStyle.scaled(15))
            Text(member ?? label).font(textStyle.system(12, weight: .semibold)).foregroundStyle(Semantic.ink)
            if member != nil { Text(label).font(textStyle.system(11.5)).foregroundStyle(Semantic.secondaryInk) }
            if live { ProgressView().controlSize(.mini).tint(accent).accessibilityLabel("Working") }
        }
        .lineLimit(1)
        .accessibilityElement(children: .combine)
        .accessibilityAddTraits(.isHeader)
    }
}

/// The user's turn, as a bubble on the trailing edge.
struct UserRow: View {
    var entry: ChatEntry
    var title = "You"
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        VStack(alignment: .trailing, spacing: 4) {
            if title != "You" {
                Text(title).font(textStyle.system(10.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
            }
            VStack(alignment: .leading, spacing: textStyle.scaled(6)) {
                if let attachments = entry.attachments, !attachments.isEmpty { ChatAttachmentStrip(attachments: attachments) }
                if !entry.text.isEmpty {
                    ChatSelectableText(text: AttributedString(entry.text), font: textStyle.nsFont, color: Semantic.nsInk, message: entry.text)
                }
            }
            .padding(.horizontal, textStyle.scaled(13)).padding(.vertical, textStyle.scaled(8))
            .background(RoundedRectangle(cornerRadius: textStyle.scaled(15), style: .continuous).fill(Semantic.selection))
        }
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
        }
        .frame(maxWidth: .infinity, alignment: .leading)
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
            if !detail.isEmpty || command != nil {
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
                .background(RoundedRectangle(cornerRadius: 8, style: .continuous).fill(Semantic.surface.opacity(0.6)))
                .overlay(RoundedRectangle(cornerRadius: 8, style: .continuous).stroke(Semantic.hairline, lineWidth: 1))
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
        let files = Set(entries.flatMap(\.changedFiles)).count
        let counted = entries.compactMap { ChatPatchStats.count(chatPatchText($0)) }
        let stats = counted.isEmpty ? nil : counted.reduce(ChatPatchStats(), +)
        var glyphs: [String] = []
        for entry in entries where !glyphs.contains(ChatToolDisplay.glyph(entry.tool)) { glyphs.append(ChatToolDisplay.glyph(entry.tool)) }
        return HStack(spacing: textStyle.scaled(7)) {
            HStack(spacing: textStyle.scaled(1)) {
                ForEach(glyphs.prefix(3), id: \.self) { ChatToolGlyph(name: $0, size: textStyle.scaled(15)).foregroundStyle(accent) }
            }
            ChatShimmerText(text: live ? "Working" : "Worked", active: live, accent: accent, base: Semantic.ink)
                .font(textStyle.system(12, weight: .medium)).fixedSize()
            Text("· " + ChatToolDisplay.activity(entries.map(\.tool)) + (files > 0 ? " · \(files) \(files == 1 ? "file" : "files")" : ""))
                .font(textStyle.system(11.5)).foregroundStyle(Semantic.secondaryInk).lineLimit(1)
            if let stats { ChatPatchStatsLabel(stats: stats, size: textStyle.scaled(11)) }
            if entries.contains(where: \.isError) {
                Image(systemName: "exclamationmark.triangle.fill").font(textStyle.system(10)).foregroundStyle(Color(nsColor: .systemRed))
            }
            if !open, live, let latest = entries.last {
                let step = ChatToolDisplay.describe(tool: latest.tool, summary: latest.summary ?? latest.text)
                Text(step.verb + (step.subject.isEmpty ? "" : " " + step.subject)).font(textStyle.system(11.5))
                    .foregroundStyle(.tertiary).lineLimit(1).truncationMode(.middle)
            }
            Spacer(minLength: 0)
            Image(systemName: "chevron.right").font(textStyle.system(8.5, weight: .semibold)).foregroundStyle(Semantic.secondaryInk)
                .rotationEffect(.degrees(open ? 90 : 0)).opacity(hovering || open ? 1 : 0.4)
        }
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
    private static let lineLimit = 600

    var body: some View {
        Text(attributed).font(.system(size: size, design: .monospaced)).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var attributed: AttributedString {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        let isPatch = lines.contains { $0.hasPrefix("@@") || $0.hasPrefix("+++ ") || $0.hasPrefix("--- ") || $0.hasPrefix("*** ") }
        var result = AttributedString()
        for (index, line) in lines.prefix(Self.lineLimit).enumerated() {
            var piece = AttributedString(String(line))
            piece.foregroundColor = isPatch ? colour(line) : ink
            result.append(piece)
            if index < lines.count - 1 { result.append(AttributedString("\n")) }
        }
        if lines.count > Self.lineLimit {
            var more = AttributedString("… \(lines.count - Self.lineLimit) more lines")
            more.foregroundColor = secondaryInk
            result.append(more)
        }
        return result
    }

    private var ink: Color { onDark ? .white.opacity(0.88) : Semantic.ink }
    private var secondaryInk: Color { onDark ? .white.opacity(0.5) : Semantic.secondaryInk }

    private func colour(_ line: Substring) -> Color {
        if line.hasPrefix("+++") || line.hasPrefix("---") || line.hasPrefix("@@") || line.hasPrefix("*** ") { return secondaryInk }
        if line.hasPrefix("+") { return Color(nsColor: .systemGreen) }
        if line.hasPrefix("-") { return Color(nsColor: .systemRed) }
        return ink
    }
}

/// A notice as a quiet divider. A Team checkpoint reads as a handoff that
/// names the member who paused; the runtime's full wording stays in the tooltip.
struct NoticeRow: View {
    var entry: ChatEntry
    var member: String? = nil
    @Environment(\.chatTextStyle) private var textStyle

    var body: some View {
        let checkpoint = ChatNotice.style(entry.text) == .checkpoint
        HStack(spacing: textStyle.scaled(10)) {
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
