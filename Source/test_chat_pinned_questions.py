"""The pinned question stack above the composer: one card at a time, a queue
that hands over when a question is answered, and drafts that never leak from
one question into the next."""
from pathlib import Path
import platform
import subprocess
import tempfile
import unittest


STUBS = r'''
import SwiftUI

enum HubTheme {
    enum Semantic {
        static let ink = Color.primary
        static let secondaryInk = Color.secondary
        static let raisedSurface = Color.primary.opacity(0.045)
    }
    enum Radius { static let panel: CGFloat = 14 }
}

struct ChatTextStyle {
    var size: Double = 13
    func scaled(_ value: CGFloat) -> CGFloat { value }
    func system(_ size: CGFloat, weight: Font.Weight = .regular, design: Font.Design = .default) -> Font {
        .system(size: size, weight: weight, design: design)
    }
}
private struct ChatTextStyleKey: EnvironmentKey { static let defaultValue = ChatTextStyle() }
extension EnvironmentValues {
    var chatTextStyle: ChatTextStyle {
        get { self[ChatTextStyleKey.self] }
        set { self[ChatTextStyleKey.self] = newValue }
    }
}

extension View {
    func hubCard(cornerRadius: CGFloat, accent: Color? = nil) -> some View { self }
}

struct ChatQuestionRecord: Decodable, Identifiable {
    struct Option: Decodable, Identifiable { var label: String; var description: String; var id: String { label } }
    struct Item: Decodable, Identifiable {
        var id: String; var header: String; var question: String; var options: [Option]; var multiSelect: Bool
    }
    var id: String; var status: String; var mode: String; var asker: String
    var questions: [Item]; var answers: [String: [String]]
}

struct ChatEntry: Decodable, Identifiable {
    var id: String
    var kind: String
    var route: String
    var memberID: String?
    var memberName: String?
    var question: ChatQuestionRecord?
}

@MainActor final class ChatModel: ObservableObject {
    @Published var entries: [ChatEntry] = []
    @Published var connected = true
    var answers: [(String, [String: [String]], Bool)] = []
    func accent(for route: String) -> Color { .accentColor }
    func answerQuestion(_ question: ChatQuestionRecord, answers: [String: [String]], dismiss: Bool = false) {
        self.answers.append((question.id, answers, dismiss))
    }
}
'''

CASES = r'''
import AppKit
import SwiftUI
import Combine

final class Feed: ObservableObject {
    @Published var pending: [ChatPendingQuestion] = []
    var answers: [(String, [String: [String]], Bool)] = []
}

struct Harness: View {
    @ObservedObject var feed: Feed
    var body: some View {
        ChatPinnedQuestionStack(questions: feed.pending, accent: { _ in .accentColor }, enabled: true,
                                onAnswer: { card, answers, dismiss in feed.answers.append((card.id, answers, dismiss)) })
    }
}

@main struct Cases {
    @MainActor static func main() {
        let app = NSApplication.shared
        app.setActivationPolicy(.prohibited)
        func check(_ condition: Bool, _ message: String) { if !condition { fatalError(message) } }
        func settle(_ seconds: Double = 0.1) { RunLoop.main.run(until: Date().addingTimeInterval(seconds)) }

        func record(_ id: String, asker: String, mode: String = "async", status: String = "pending",
                    items: Int = 1, options: Int = 1) -> ChatQuestionRecord {
            ChatQuestionRecord(id: id, status: status, mode: mode, asker: asker,
                questions: (0..<items).map { index in
                    ChatQuestionRecord.Item(id: "\(id)-\(index)", header: "", question: "Which one?",
                                            options: (0..<options).map {
                                                ChatQuestionRecord.Option(label: "Option \($0)", description: "") },
                                            multiSelect: false)
                },
                answers: [:])
        }
        func entry(_ id: String, kind: String = "question", route: String = "codex/sol",
                   member: String? = nil, question: ChatQuestionRecord?) -> ChatEntry {
            ChatEntry(id: id, kind: kind, route: route, memberID: member, memberName: member, question: question)
        }

        // The model hands over only the questions still waiting, in the order
        // they were asked, with the row they came from.
        let model = ChatModel()
        model.entries = [
            entry("e1", kind: "user", question: nil),
            entry("e2", route: "codex/sol", member: "sol", question: record("q1", asker: "Sol")),
            entry("e3", route: "claude/nova", member: "nova", question: record("q2", asker: "Nova")),
            entry("e4", route: "claude/nova", member: "nova", question: record("q3", asker: "Nova", status: "answered")),
            entry("e5", route: "codex/sol", member: "sol", question: record("q4", asker: "Sol", mode: "wait")),
        ]
        let pending = model.pendingQuestions()
        check(pending.count == 3, "Pending questions: \(pending.count)")
        check(pending.map(\.id) == ["q1", "q2", "q4"], "Order changed: \(pending.map(\.id))")
        check(pending[0].entryID == "e2", "The row a question came from was lost")
        check(pending[0].route == "codex/sol" && pending[0].memberID == "sol", "The asker's route was lost")
        check(pending[0].asker == "Sol", "The asker's name was lost")
        check(pending[0].itemCount == 1 && pending[0].itemLabel == "1 question", "The item count was lost")
        check(pending[2].isAsync == false, "A waiting question read as async")
        check(pending[0].isAsync, "An async question read as waiting")

        // The stack shows one card at a time and walks the queue.
        var queue = ChatQuestionQueue()
        check(queue.visible(in: pending)?.id == "q1", "The oldest question did not come first")
        queue.update(pending)
        check(queue.visible(in: pending)?.id == "q1", "Reading the queue moved the card")
        queue.step(1, in: pending)
        check(queue.visible(in: pending)?.id == "q2", "Stepping forward did not move the card")
        queue.step(1, in: pending)
        check(queue.visible(in: pending)?.id == "q4", "Stepping forward did not move the card")
        queue.step(1, in: pending)
        check(queue.visible(in: pending)?.id == "q4", "Stepping walked past the end of the queue")
        queue.step(-1, in: pending)
        check(queue.visible(in: pending)?.id == "q2", "Stepping back did not move the card")
        queue.step(-1, in: pending)
        check(queue.visible(in: pending)?.id == "q1", "Stepping back did not move the card")
        queue.step(-1, in: pending)
        check(queue.visible(in: pending)?.id == "q1", "Stepping walked past the start of the queue")

        // A card that has just been answered hands over to the question that
        // took its place rather than jumping back to the oldest.
        var handover = ChatQuestionQueue()
        handover.update(pending)
        let answered = pending.filter { $0.id != "q1" }
        handover.update(answered)
        check(handover.visible(in: answered)?.id == "q2", "Answering did not hand over to the next question")

        // Answering a question the reader was not looking at leaves them where
        // they were.
        var steady = ChatQuestionQueue()
        steady.update(pending)
        steady.step(1, in: pending)
        check(steady.visible(in: pending)?.id == "q2", "The reader did not move to the second question")
        let afterFirst = pending.filter { $0.id != "q1" }
        steady.update(afterFirst)
        check(steady.visible(in: afterFirst)?.id == "q2", "Another question's answer moved the reader")

        // A question that arrives while the reader is on another card leaves
        // them where they are.
        var arriving = ChatQuestionQueue()
        arriving.update(pending)
        arriving.step(1, in: pending)
        let grew = pending + [ChatPendingQuestion(entryID: "e6", route: "codex/sol", memberID: "sol",
                                                   record: record("q5", asker: "Sol"))]
        arriving.update(grew)
        check(arriving.visible(in: grew)?.id == "q2", "A new question moved the reader")
        check(grew.count == 4, "The queue lost the question that arrived")

        // The count reads as a position in the queue.
        check(ChatQuestionQueue.position(index: 0, count: 3) == "1 of 3", "Position header wrong")
        check(ChatQuestionQueue.position(index: 2, count: 3) == "3 of 3", "Position header wrong")
        check(ChatQuestionQueue.position(index: 0, count: 1) == "1 of 1", "Position header wrong")
        check(ChatQuestionQueue.position(index: 0, count: 0) == "", "An empty queue still counted")

        // Drafts belong to one question: they survive moving between cards
        // and go when the question is answered.
        let drafts = QuestionDrafts.shared
        drafts.answers["q1", default: [:]]["colour"] = ["Blue"]
        drafts.text["q1:colour"] = "Blue"
        check(drafts.answers["q1"]?["colour"] == ["Blue"], "The answer draft was lost")
        check(drafts.answers["q2"] == nil, "A draft leaked into another question")
        drafts.clear("q1")
        check(drafts.answers["q1"] == nil, "An answered question kept its draft")
        check(drafts.text["q1:colour"] == nil, "An answered question kept its text")

        // The stack renders: nothing without a question, one card with them.
        let feed = Feed()
        let host = NSHostingView(rootView: Harness(feed: feed))
        let window = NSWindow(contentRect: NSRect(x: 0, y: 0, width: 520, height: 400),
                              styleMask: [.titled], backing: .buffered, defer: false)
        window.contentView = host
        window.setFrameOrigin(NSPoint(x: -20000, y: -20000))
        window.orderFrontRegardless()
        settle()
        host.layoutSubtreeIfNeeded()
        settle()
        check(host.subviews.isEmpty, "The stack with no question waiting still rendered a card")

        feed.pending = [
            ChatPendingQuestion(entryID: "e2", route: "codex/sol", memberID: "sol", record: record("q1", asker: "Sol")),
            ChatPendingQuestion(entryID: "e3", route: "claude/nova", memberID: "nova", record: record("q2", asker: "Nova")),
        ]
        settle()
        host.layoutSubtreeIfNeeded()
        settle()
        check(host.subviews.isEmpty == false, "The stack with a question waiting rendered nothing")

        // A card that bundles several questions and their options stays
        // capped, so the composer and the transcript keep their room.
        feed.pending = [ChatPendingQuestion(entryID: "e9", route: "codex/sol", memberID: "sol",
                                             record: record("qt", asker: "Sol", items: 4, options: 6))]
        settle()
        host.layoutSubtreeIfNeeded()
        settle()
        let tall = host.fittingSize.height
        check(tall > 100, "The tall card did not render its content: \(tall)")
        check(tall <= 360, "A tall card grew past its cap: \(tall)")

        // The answer a card sends carries the question it was asked for.
        model.answerQuestion(record("q1", asker: "Sol"), answers: ["colour": ["Blue"]], dismiss: false)
        check(model.answers.count == 1 && model.answers[0].0 == "q1", "The answer lost its question")
        check(model.answers[0].1["colour"] == ["Blue"], "The answer lost its values")

        print("Pinned question stack checks passed")
    }
}
'''


class PinnedQuestionTests(unittest.TestCase):
    def run_native(self, files, cases, marker, stubs=None):
        source = Path(__file__).parent
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            extra = []
            if stubs is not None:
                (root / "Stubs.swift").write_text(stubs)
                extra.append(str(root / "Stubs.swift"))
            (root / "Cases.swift").write_text(cases)
            binary = root / "pinned-question-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-swift-version", "5", "-parse-as-library",
                "-target", platform.machine() + "-apple-macosx14.0", "-module-cache-path", str(root / "cache"),
                *extra, *(str(source / name) for name in files),
                str(root / "Cases.swift"), "-framework", "AppKit", "-framework", "SwiftUI", "-o", str(binary)],
                capture_output=True, text=True, timeout=180)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=60)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn(marker, ran.stdout)

    def test_pinned_question_stack_keeps_one_card_and_its_queue(self):
        self.run_native(["ChatQuestions.swift"], CASES,
                        "Pinned question stack checks passed", stubs=STUBS)


if __name__ == "__main__":
    unittest.main()
