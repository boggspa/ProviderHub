import SwiftUI

/// A one-second refresh scoped to the footer, never the transcript/model loop.
struct ChatTurnTime: View {
    @ObservedObject var model: ChatModel
    var body: some View {
        Group {
            if model.turnStartedAt != nil {
                TimelineView(.periodic(from: .now, by: 1)) { _ in value }
            } else { value }
        }.font(.system(size: 10.5)).foregroundStyle(HubTheme.Semantic.secondaryInk)
            .accessibilityElement(children: .combine)
    }
    private var value: some View {
        HStack(spacing: 6) {
            Text("Turn time")
            Text(model.turnTimecode).monospacedDigit()
        }.fixedSize().accessibilityLabel("Turn time " + model.turnTimecode)
    }
}
