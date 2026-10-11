import Foundation
import XCTest
@testable import ProviderHubRemote

final class QuestionTests: XCTestCase {
    final class Link: CompanionLink {
        var commands: [RemoteCommand] = []
        func send(_ command: RemoteCommand) { commands.append(command) }
    }
    func testQuestionProjectionAndExactAnswerCommand() throws {
        let row: [String: Any] = ["id":"card-row", "role":"question", "question":[
            "id":"question-identity", "status":"pending", "mode":"async", "asker":"Sol", "answers":[:],
            "questions":[["id":"colour", "header":"Colour", "question":"Which colour?", "multiSelect":false,
                          "options":[["label":"Blue", "description":"Cool"]]]]]]
        var store = CompanionStore()
        try store.receive(["method":"session.snapshot", "params":["cursor":0, "chats":[], "selected":["id":"chat-a", "entries":[row]]]])
        let question = try XCTUnwrap(store.entries.first?.question)
        XCTAssertEqual(question.asker, "Sol")
        XCTAssertEqual(question.questions.first?.options.first?.label, "Blue")
        let link = Link(), model = CompanionModel()
        model.link = link; model.status = "Connected"; model.selectedChatID = "another-chat"
        model.answerQuestion(question, chat:"chat-a", answers:["colour":["Blue"]], dismiss:false)
        let command = try XCTUnwrap(link.commands.first)
        XCTAssertEqual(command.chat, "chat-a")
        XCTAssertEqual(command.questionId, "question-identity")
        XCTAssertEqual(command.answers, ["colour":["Blue"]])
        XCTAssertEqual(command.method, "chat.answer_question")
        var settled = row
        var body = try XCTUnwrap(row["question"] as? [String: Any])
        body["status"] = "answered"; body["answers"] = ["colour":["Blue"]]; settled["question"] = body
        try store.receive(["method":"session.delta", "params":["cursor":1, "event":["event":"entry", "chat":"chat-a", "entry":settled]]])
        XCTAssertEqual(store.entries.count, 1)
        XCTAssertEqual(store.entries.first?.question?.status, "answered")
        model.status = "Disconnected"
        model.answerQuestion(question, chat:"chat-a", answers:[:], dismiss:true)
        XCTAssertEqual(link.commands.count, 1)
    }
}
