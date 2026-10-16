import SwiftUI
#if os(iOS)
import UIKit
#else
import AppKit
#endif

/// The same collective-turn table in both transcripts, including per-file
/// expanders and the six-row preview. The caller owns optional platform actions.
struct ChatSharedTurnChanges<Action: View>: View {
    var changes: ChatTurnChanges
    var scale: CGFloat = 1
    var ink: Color = .primary
    var secondary: Color = .secondary
    var hairline: Color
    var fill: Color
    var rim: Color
    @ViewBuilder var action: (String) -> Action
    @State private var showAll = false
    @State private var expanded = Set<String>()
    var body: some View {
        let files = showAll ? changes.files : Array(changes.files.prefix(6))
        VStack(alignment: .leading, spacing: 0) {
            HStack(spacing: 7 * scale) {
                ChatToolGlyph(name: "apply_patch", size: 14 * scale).foregroundStyle(secondary)
                Text(changes.files.count == 1 ? "1 file changed" : "\(changes.files.count) files changed").font(.system(size: 12 * scale, weight: .medium))
                Text("this turn").font(.system(size: 11.5 * scale)).foregroundStyle(secondary)
                Spacer(minLength: 0)
                stats(changes.added, changes.removed)
            }.padding(.horizontal, 10).padding(.vertical, 7 * scale)
            ForEach(files) { file in
                Rectangle().fill(hairline).frame(height: 1)
                Button { if expanded.contains(file.path) { expanded.remove(file.path) } else { expanded.insert(file.path) } } label: {
                    HStack(spacing: 6 * scale) {
                        Text((file.path as NSString).lastPathComponent).font(.system(size: 11.5 * scale, weight: .medium, design: .monospaced)).lineLimit(1).layoutPriority(1)
                        Text((file.path as NSString).deletingLastPathComponent).font(.system(size: 11 * scale, design: .monospaced)).foregroundStyle(.tertiary).lineLimit(1).truncationMode(.head)
                        Spacer(minLength: 6)
                        Text(file.members.joined(separator: ", ")).font(.system(size: 11 * scale)).foregroundStyle(secondary).lineLimit(1)
                        stats(file.added, file.removed)
                        Image(systemName: "chevron.right").font(.system(size: 8.5 * scale, weight: .semibold)).rotationEffect(.degrees(expanded.contains(file.path) ? 90 : 0)).foregroundStyle(secondary)
                    }.padding(.horizontal, 10).padding(.vertical, 5 * scale).contentShape(Rectangle())
                }.buttonStyle(.plain).accessibilityLabel(file.path + ", changed by " + file.members.joined(separator: ", "))
                if expanded.contains(file.path) {
                    VStack(alignment: .leading, spacing: 6 * scale) {
                        if file.diff.isEmpty { Text("No textual diff was recorded for this file.").font(.system(size: 11.5 * scale)).foregroundStyle(secondary) }
                        else { ScrollView(.horizontal) { ChatSharedPatchText(text: file.diff, size: 11 * scale, ink: ink, secondaryInk: secondary) } }
                        action(file.path)
                    }.padding(10)
                }
            }
            if changes.files.count > files.count {
                Rectangle().fill(hairline).frame(height: 1)
                Button("Show \(changes.files.count - files.count) more") { showAll = true }.buttonStyle(.plain).font(.system(size: 11.5 * scale)).foregroundStyle(secondary).padding(.horizontal, 10).padding(.vertical, 6 * scale)
            }
        }.foregroundStyle(ink).background(RoundedRectangle(cornerRadius: 8).fill(fill))
            .overlay(RoundedRectangle(cornerRadius: 8).strokeBorder(rim, lineWidth: 0.5))
    }
    @ViewBuilder private func stats(_ added: Int, _ removed: Int) -> some View {
        if added + removed > 0 {
            HStack(spacing: 3) { Text("+\(added)").foregroundStyle(ChatSharedPatchText.added); Text("−\(removed)").foregroundStyle(ChatSharedPatchText.removed) }
                .font(.system(size: 11 * scale, design: .monospaced)).fixedSize()
        }
    }
}

struct ChatSharedBlackboardPost<Mark: View>: View {
    var author: String
    var key: String
    var text: String
    var category: String
    var quote: String = ""
    var quoteAuthor: String = ""
    var updated: Date?
    var accent: Color
    var ink: Color
    var secondary: Color
    var fill: Color
    var rim: Color
    @ViewBuilder var mark: () -> Mark
    @State private var expanded = false
    var body: some View {
        VStack(alignment: .leading, spacing: 5) {
            HStack(spacing: 6) {
                HStack(spacing: 3) { mark(); Text(author).lineLimit(1) }.font(.system(size: 10.5, weight: .medium)).foregroundStyle(accent)
                Text(key).font(.system(size: 11, weight: .medium, design: .monospaced)).lineLimit(1).truncationMode(.middle)
                Spacer(minLength: 4)
                Text(category == "do-not-repeat" ? "Do not repeat" : category.capitalized).font(.system(size: 9.5, weight: .medium)).foregroundStyle(tint).fixedSize()
            }
            Text(text).font(.system(size: 11.5)).lineLimit(expanded ? nil : 3).fixedSize(horizontal: false, vertical: true)
            if !quote.isEmpty {
                HStack(alignment: .top, spacing: 6) {
                    RoundedRectangle(cornerRadius: 1).fill(rim).frame(width: 2)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(quoteAuthor).font(.system(size: 10)); Text(quote).font(.system(size: 10.5)).lineLimit(expanded ? nil : 2)
                    }.foregroundStyle(secondary)
                }.fixedSize(horizontal: false, vertical: true)
            }
            HStack {
                if let updated { Text(updated.formatted(.relative(presentation: .named))).font(.system(size: 9.5)).foregroundStyle(secondary) }
                Spacer()
                if text.count > 160 || text.contains("\n") { Image(systemName: expanded ? "chevron.up" : "chevron.down").font(.system(size: 8.5)).foregroundStyle(secondary) }
            }
        }.padding(8).frame(maxWidth: .infinity, alignment: .leading).foregroundStyle(ink)
            .background(RoundedRectangle(cornerRadius: 7).fill(fill)).overlay(RoundedRectangle(cornerRadius: 7).strokeBorder(rim, lineWidth: 0.5))
            .contentShape(Rectangle()).onTapGesture { withAnimation(.easeOut(duration: 0.12)) { expanded.toggle() } }
            .accessibilityElement(children: .combine).accessibilityHint("Tap to expand or collapse")
    }
    private var tint: Color {
        switch category { case "decision": return .blue; case "fact": return .green; case "risk": return .orange; case "do-not-repeat": return .red; default: return secondary }
    }
}

/// Canonical patch renderer for Mac and iOS. Selection copies plain diff text.
struct ChatSharedPatchText: View {
    var text: String
    var onDark = false
    var size: CGFloat = 11.5
    var ink: Color = .primary
    var secondaryInk: Color = .secondary
    static let lineLimit = 600

    static var added: Color {
        #if os(iOS)
        return Color(uiColor: .systemGreen)
        #else
        return Color(nsColor: .systemGreen)
        #endif
    }
    static var removed: Color {
        #if os(iOS)
        return Color(uiColor: .systemRed)
        #else
        return Color(nsColor: .systemRed)
        #endif
    }
    var body: some View {
        Text(attributed).font(.system(size: size, design: .monospaced)).textSelection(.enabled)
            .frame(maxWidth: .infinity, alignment: .leading)
    }
    var attributed: AttributedString {
        let lines = text.split(separator: "\n", omittingEmptySubsequences: false)
        let patch = lines.contains { $0.hasPrefix("@@") || $0.hasPrefix("+++ ") || $0.hasPrefix("--- ") || $0.hasPrefix("*** ") }
        var result = AttributedString()
        for (index, line) in lines.prefix(Self.lineLimit).enumerated() {
            var piece = AttributedString(String(line))
            piece.foregroundColor = patch ? color(line) : foreground
            result.append(piece)
            if index < lines.count - 1 { result.append(AttributedString("\n")) }
        }
        if lines.count > Self.lineLimit {
            var more = AttributedString("… \(lines.count - Self.lineLimit) more lines")
            more.foregroundColor = subdued; result.append(more)
        }
        return result
    }
    private var foreground: Color { onDark ? .white.opacity(0.88) : ink }
    private var subdued: Color { onDark ? .white.opacity(0.5) : secondaryInk }
    private func color(_ line: Substring) -> Color {
        if line.hasPrefix("+++") || line.hasPrefix("---") || line.hasPrefix("@@") || line.hasPrefix("*** ") { return subdued }
        if line.hasPrefix("+") { return Self.added }
        if line.hasPrefix("-") { return Self.removed }
        return foreground
    }
}

struct ChatSharedDiffDisclosure<Author: View>: View {
    var path: String
    var oldPath: String?
    var added: Int
    var deleted: Int
    var binary: Bool
    var diff: String
    @Binding var expanded: Bool
    @ViewBuilder var author: () -> Author
    var body: some View {
        DisclosureGroup(isExpanded: $expanded) {
            if binary { Text("Binary file").font(.system(size: 11)).foregroundStyle(.secondary).padding(.vertical, 6) }
            else {
                ScrollView([.horizontal, .vertical]) {
                    ChatSharedPatchText(text: diff, onDark: true).padding(10).fixedSize(horizontal: true, vertical: true)
                }
                .frame(minHeight: 80, maxHeight: 300)
                .background(Color.black.opacity(0.88), in: RoundedRectangle(cornerRadius: 6))
                .padding(.top, 5)
            }
        } label: {
            HStack(spacing: 5) {
                Text(path).font(.system(size: 11.5)).lineLimit(1).truncationMode(.middle)
                author()
                Spacer(minLength: 0)
                Text("+\(added)").foregroundStyle(ChatSharedPatchText.added)
                Text("−\(deleted)").foregroundStyle(ChatSharedPatchText.removed)
            }.font(.system(size: 10.5, design: .monospaced))
                .help(oldPath.map { $0 + " → " + path } ?? path)
        }
    }
}

struct ChatSharedMemberControl: View {
    var name: String
    var start: Bool
    var disabled = false
    var action: () -> Void
    var body: some View {
        Button(action: action) { Image(systemName: start ? "play.circle" : "stop.circle").font(.system(size: 13))
            #if os(iOS)
                .frame(minWidth: 32, minHeight: 32).contentShape(Rectangle())
            #endif
        }.buttonStyle(.plain).foregroundStyle(.secondary).disabled(disabled)
            .help(start ? "Start another contribution with the current Team context" : "Stop this member’s current run")
            .accessibilityLabel((start ? "Start " : "Stop ") + name)
    }
}

struct ChatSharedMemberRow<Mark: View>: View {
    var name: String
    var model: String
    var status: String
    var responsibility: String
    var nextStep: String
    var failure: String
    var waitReason: String
    var pendingLabel: String?
    var pending: Bool
    var used: Int?
    var limit: Int?
    var canControl: Bool
    var stoppedByUser = false
    var needsInput = false
    var accent: Color
    var fill: Color
    var ink: Color = .primary
    var secondary: Color = .secondary
    var control: (Bool) -> Void
    @ViewBuilder var mark: () -> Mark
    private var label: String {
        if pending { return "Applying…" }
        if status == "working", stoppedByUser { return "Stopping…" }
        return status.replacingOccurrences(of: "_", with: " ").capitalized
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            HStack(spacing: 6) {
                mark()
                Text(name).font(.system(size: 12, weight: .medium)).foregroundStyle(ink)
                Spacer(minLength: 0)
                if status == "working" || pending { ProgressView().controlSize(.mini) }
                Text(label).font(.system(size: 10.5)).foregroundStyle(status == "error" ? ChatSharedPatchText.removed : secondary)
                if status == "working" {
                    ChatSharedMemberControl(name: name, start: false, disabled: !canControl || stoppedByUser) { control(false) }
                } else if ["done", "ready", "stopped", "interrupted", "error", "limit_reached"].contains(status) {
                    ChatSharedMemberControl(name: name, start: true, disabled: !canControl || needsInput || pending) { control(true) }
                }
            }
            Text(model).font(.system(size: 10.5)).foregroundStyle(secondary).lineLimit(2)
            if let pendingLabel { Text("→ " + pendingLabel).font(.system(size: 10.5)).foregroundStyle(secondary).lineLimit(1) }
            if !responsibility.isEmpty { Text(responsibility).font(.system(size: 11.5)).lineLimit(3) }
            if status == "error", !failure.isEmpty { Text(failure).font(.system(size: 11.5)).foregroundStyle(ChatSharedPatchText.removed).lineLimit(2).textSelection(.enabled) }
            else if !nextStep.isEmpty, status != "done" { Text(nextStep).font(.system(size: 11.5)).foregroundStyle(secondary).textSelection(.enabled) }
            if !waitReason.isEmpty, status != "done" { Text(waitReason).font(.system(size: 10.5)).foregroundStyle(secondary).textSelection(.enabled) }
            if let used {
                Text("Context \(used.formatted())" + (limit.map { " / \($0.formatted())" } ?? ""))
                    .font(.system(size: 10, design: .monospaced)).foregroundStyle(secondary)
            }
        }.padding(8).frame(maxWidth: .infinity, alignment: .leading)
            .background(RoundedRectangle(cornerRadius: 7, style: .continuous).fill(fill))
            .overlay(RoundedRectangle(cornerRadius: 7, style: .continuous).strokeBorder(accent.opacity(0.22), lineWidth: 0.5))
    }
}

struct ChatSharedInspectorTabs: View {
    struct Item: Identifiable { var id: String; var label: String; var title: String; var icon: String; var count = 0 }
    var items: [Item]
    var selected: String
    var accent: Color
    var choose: (String) -> Void
    var body: some View {
        ViewThatFits(in: .horizontal) { bar(compact: false); bar(compact: true) }
            .frame(maxWidth: .infinity, alignment: .leading).padding(.horizontal, 14).padding(.top, 12)
    }
    private func bar(compact: Bool) -> some View {
        HStack(spacing: compact ? 14 : 12) {
            ForEach(items) { item in
                let active = item.id == selected
                Button { choose(item.id) } label: {
                    VStack(spacing: 8) {
                        HStack(spacing: 4) {
                            if active || !compact { Label(item.label, systemImage: item.icon) } else { Image(systemName: item.icon) }
                            if item.count > 0 {
                                Text("\(item.count)").font(.system(size: 9.5, weight: .semibold)).monospacedDigit()
                                    .foregroundStyle(accent).padding(.horizontal, 4.5).padding(.vertical, 1).background(accent.opacity(0.14), in: Capsule())
                            }
                        }.font(.system(size: 11.5, weight: active ? .medium : .regular)).lineLimit(1).fixedSize()
                        Rectangle().fill(active ? accent : .clear).frame(height: 2)
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).foregroundStyle(active ? .primary : .secondary).help(item.title)
                    .accessibilityLabel(item.title).accessibilityValue(item.count > 0 ? "\(item.count) running" : "")
                    .accessibilityAddTraits(active ? .isSelected : [])
            }
        }
    }
}

struct ChatSharedProcessRow<Mark: View, Status: View, Detail: View>: View {
    var owner: String
    var command: String
    var canStop: Bool
    var stopDisabled = false
    @Binding var expanded: Bool
    var stop: () -> Void
    @ViewBuilder var mark: () -> Mark
    @ViewBuilder var status: () -> Status
    @ViewBuilder var detail: () -> Detail
    var body: some View {
        VStack(alignment: .leading, spacing: 7) {
            HStack(alignment: .top, spacing: 6) {
                Button { expanded.toggle() } label: {
                    HStack(alignment: .top, spacing: 6) {
                        Image(systemName: "chevron.right").font(.system(size: 8.5, weight: .semibold)).foregroundStyle(.secondary)
                            .rotationEffect(.degrees(expanded ? 90 : 0)).frame(width: 10, height: 15)
                        mark().padding(.top, 0.5)
                        VStack(alignment: .leading, spacing: 3) {
                            HStack(spacing: 6) {
                                Text(owner).font(.system(size: 11.5, weight: .medium)).lineLimit(1)
                                Spacer(minLength: 4)
                                status()
                            }
                            Text(command).font(.system(size: 11, design: .monospaced)).lineLimit(2).truncationMode(.middle)
                                .frame(maxWidth: .infinity, alignment: .leading)
                        }
                    }.contentShape(Rectangle())
                }.buttonStyle(.plain).help(command).accessibilityLabel(owner + ": " + command)
                    .accessibilityHint(expanded ? "Hides output" : "Shows output")
                if canStop { ChatSharedMemberControl(name: command, start: false, disabled: stopDisabled, action: stop).padding(.top, 0.5) }
            }
            if expanded { detail().padding(.leading, 16) }
        }
    }
}
