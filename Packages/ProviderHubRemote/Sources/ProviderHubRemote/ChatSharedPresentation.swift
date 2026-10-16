import SwiftUI

/// Shared chrome primitives. The Mac supplies its theme/font scale and the
/// companion supplies the same semantics with iOS Dynamic Type scaling.
struct ChatSharedSpeakerHeader<Mark: View>: View {
    var member: String?
    var label: String
    var timestamp: String?
    var accent: Color
    var ink: Color
    var secondary: Color
    var live: Bool
    var scale: CGFloat = 1
    @ViewBuilder var mark: () -> Mark
    var body: some View {
        HStack(spacing: 7 * scale) {
            mark()
            Text(member ?? label).font(.system(size: 12 * scale, weight: .semibold)).foregroundStyle(ink)
            if member != nil { Text(label).font(.system(size: 11.5 * scale)).foregroundStyle(secondary) }
            if let timestamp { Text(timestamp).font(.system(size: 10.5 * scale)).foregroundStyle(secondary) }
            if live { ProgressView().controlSize(.mini).tint(accent).accessibilityLabel("Working") }
        }.lineLimit(1).accessibilityElement(children: .combine).accessibilityAddTraits(.isHeader)
    }
}

struct ChatSharedMemberChip<Mark: View>: View {
    var name: String
    var model: String = ""
    var used: Int?
    var limit: Int?
    var active: Bool
    var named = true
    var counted = true
    var capped = false
    var accent: Color
    var ink: Color
    var secondary: Color
    @ViewBuilder var mark: () -> Mark
    var body: some View {
        HStack(spacing: 5) {
            mark()
            ring
            if named {
                Text(name).font(.system(size: 10.5, weight: active ? .semibold : .regular))
                    .foregroundStyle(active ? ink : secondary).lineLimit(1).truncationMode(.tail)
                    .frame(maxWidth: capped ? 84 : nil, alignment: .leading)
            }
            if counted, let used { Text(Self.tokens(used)).font(.system(size: 10.5, design: .monospaced)).foregroundStyle(secondary) }
        }.lineLimit(1)
        .accessibilityElement(children: .ignore)
        .accessibilityLabel(accessibilityText)
    }
    private var accessibilityText: String {
        let readout = used.map { Self.tokens($0) } ?? "not started"
        let identity = model.isEmpty ? name : name + " · " + model
        return identity + " context " + readout + (active ? ", working" : "")
    }
    private var ring: some View {
        ZStack {
            Circle().stroke(accent.opacity(0.3), lineWidth: 2)
            if let used, let limit, limit > 0 {
                Circle().trim(from: 0, to: CGFloat(min(1, max(0, Double(used) / Double(limit)))))
                    .stroke(accent, style: StrokeStyle(lineWidth: 2, lineCap: .round)).rotationEffect(.degrees(-90))
            }
        }.frame(width: 12, height: 12)
    }
    static func tokens(_ count: Int) -> String { count >= 1000 ? String(format: "%.1fk", Double(count) / 1000) : String(count) }
}
