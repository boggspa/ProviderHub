import SwiftUI

/// The Mac footer's turn clock. A one-second refresh scoped to this label,
/// never the transcript; it reads 00:00 between turns, as on the Mac.
struct CompanionTurnTime: View {
    var startedAt: Date?
    var body: some View {
        Group {
            if let startedAt {
                TimelineView(.periodic(from: .now, by: 1)) { context in value(context.date.timeIntervalSince(startedAt)) }
            } else { value(0) }
        }.foregroundStyle(CompanionTheme.secondary)
    }
    private func value(_ elapsed: TimeInterval) -> some View {
        let code = Self.timecode(elapsed)
        return HStack(spacing: 6) {
            Text("Turn time")
            Text(code).monospacedDigit()
        }.fixedSize().accessibilityElement(children: .ignore).accessibilityLabel("Turn time " + code)
    }
    static func timecode(_ elapsed: TimeInterval) -> String {
        let seconds = Int(min(max(0, elapsed), Double(Int.max / 2)))
        return seconds < 3600 ? String(format: "%02d:%02d", seconds / 60, seconds % 60)
            : String(format: "%02d:%02d:%02d", seconds / 3600, (seconds / 60) % 60, seconds % 60)
    }
}
