import SwiftUI

struct CompanionTranscript: View {
    let entries: [RemoteEntry]
    let chat: RemoteChat
    @State private var followsLatest = true

    var body: some View {
        ScrollViewReader { proxy in
            ScrollView {
                LazyVStack(alignment: .leading, spacing: 20) {
                    Text("Recent conversation · hosted on your Mac")
                        .font(.caption2).foregroundStyle(CompanionTheme.secondary)
                        .frame(maxWidth: .infinity).padding(.vertical, 8)
                    ForEach(entries) { entry in
                        CompanionTranscriptRow(entry: entry, chat: chat)
                            .id(entry.id)
                    }
                    if chat.busy && entries.last?.isLive != true {
                        HStack(spacing: 8) {
                            ProgressView().controlSize(.small).tint(chat.providerAccent)
                            Text("Working…").font(.caption).foregroundStyle(CompanionTheme.secondary)
                        }.padding(.vertical, 6)
                    }
                    Color.clear.frame(height: 1).id("transcript-bottom")
                        .onAppear { followsLatest = true }.onDisappear { followsLatest = false }
                }.padding(.horizontal, CompanionTheme.margin).padding(.bottom, 12)
                    .frame(maxWidth: 760).frame(maxWidth: .infinity)
            }
            .onAppear { proxy.scrollTo("transcript-bottom", anchor: .bottom) }
            .onChange(of: entries) { _, _ in if followsLatest { proxy.scrollTo("transcript-bottom", anchor: .bottom) } }
            .overlay(alignment: .bottomTrailing) {
                if !followsLatest {
                    Button { followsLatest = true; proxy.scrollTo("transcript-bottom", anchor: .bottom) } label: {
                        Image(systemName: "arrow.down").padding(12).background(.regularMaterial, in: Circle())
                    }.buttonStyle(.plain).padding(12).accessibilityLabel("Go to latest message")
                }
            }
        }
    }
}

private struct CompanionTranscriptRow: View {
    let entry: RemoteEntry
    let chat: RemoteChat
    @State private var expanded = false
    var body: some View {
        Group {
            switch entry.role {
            case "user":
                HStack {
                    Spacer(minLength: 36)
                    Text(entry.text).textSelection(.enabled)
                        .padding(.horizontal, 14).padding(.vertical, 10)
                        .background(CompanionTheme.selection, in: RoundedRectangle(cornerRadius: 18))
                }.accessibilityLabel("You: " + entry.text)
            case "assistant":
                VStack(alignment: .leading, spacing: 9) {
                    HStack(spacing: 7) {
                        Image(systemName: "sparkle").foregroundStyle(chat.providerAccent)
                        Text(entry.memberName.isEmpty ? chat.displayModel : entry.memberName).fontWeight(.semibold)
                        if entry.isLive { ProgressView().controlSize(.mini).tint(chat.providerAccent) }
                    }.font(.caption).foregroundStyle(CompanionTheme.secondary)
                    CompanionMessageText(text: entry.text)
                }.frame(maxWidth: .infinity, alignment: .leading)
            case "tool":
                DisclosureGroup(isExpanded: $expanded) {
                    Text(entry.detail.isEmpty ? entry.text : entry.detail)
                        .font(.system(.caption, design: .monospaced)).textSelection(.enabled)
                        .frame(maxWidth: .infinity, alignment: .leading).padding(.vertical, 8)
                } label: {
                    HStack(spacing: 8) {
                        Image(systemName: entry.tool == "run_shell" ? "terminal" : "wrench.and.screwdriver")
                            .foregroundStyle(chat.providerAccent)
                        Text(entry.summary.isEmpty ? (entry.tool.isEmpty ? "Tool activity" : entry.tool) : entry.summary).lineLimit(2)
                        if entry.isError { Image(systemName: "exclamationmark.triangle").foregroundStyle(.red) }
                    }.font(.caption)
                }.tint(CompanionTheme.secondary)
            default:
                Text(entry.text).font(.caption).foregroundStyle(entry.isError ? .red : CompanionTheme.secondary)
                    .textSelection(.enabled)
            }
        }.foregroundStyle(CompanionTheme.ink)
    }
}

/// Lightweight text/code presentation. No embedded browser, HTML or renderer
/// dependency; fenced code is selectable and horizontally scrollable.
private struct CompanionMessageText: View {
    let text: String
    var body: some View {
        let blocks = text.components(separatedBy: "```")
        VStack(alignment: .leading, spacing: 12) {
            ForEach(Array(blocks.enumerated()), id: \.offset) { index, block in
                if index % 2 == 1 {
                    let code = block.split(separator: "\n", maxSplits: 1, omittingEmptySubsequences: false)
                    VStack(alignment: .leading, spacing: 6) {
                        if let language = code.first, !language.isEmpty { Text(language).font(.caption2).foregroundStyle(CompanionTheme.secondary) }
                        ScrollView(.horizontal) {
                            Text(code.count > 1 ? String(code[1]) : block)
                                .font(.system(.callout, design: .monospaced)).textSelection(.enabled)
                        }
                    }.padding(12).companionCard(cornerRadius: CompanionTheme.rowRadius)
                } else if !block.isEmpty {
                    Text((try? AttributedString(markdown: block, options: .init(interpretedSyntax: .inlineOnlyPreservingWhitespace))) ?? AttributedString(block))
                        .textSelection(.enabled).frame(maxWidth: .infinity, alignment: .leading)
                }
            }
        }
    }
}
