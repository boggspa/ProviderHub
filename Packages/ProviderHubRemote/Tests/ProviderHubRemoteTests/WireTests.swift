import CryptoKit
import Foundation
import XCTest
import Network
@testable import ProviderHubRemote

final class WireTests: XCTestCase {
    func testRealTCPWithPythonServer() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["uv", "run", "python", root.appendingPathComponent("wire_peer.py").path, "--tcp"]
        process.currentDirectoryURL = root.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let output = Pipe()
        process.standardOutput = output
        try process.run()
        defer { process.terminate(); process.waitUntilExit() }
        var line = Data()
        while let byte = try output.fileHandleForReading.read(upToCount: 1), !byte.isEmpty {
            if byte[0] == 10 { break }
            line.append(byte)
        }
        let bootstrap = try JSONDecoder().decode(RemoteBootstrap.self, from: line)
        let channel = RemoteChannel(bootstrap: bootstrap, identity: .init())
        let connection = NWConnection(host: .init(bootstrap.host), port: .init(rawValue: bootstrap.port)!, using: .tcp)
        defer { connection.cancel() }
        let done = expectation(description: "Encrypted TCP command ack")
        let network = DispatchQueue(label: "wire-test")
        channel.emit = { frame in
            var data = try JSONSerialization.data(withJSONObject: frame)
            data.append(10)
            connection.send(content: data, completion: .contentProcessed { _ in })
        }
        var store = CompanionStore()
        channel.onMessage = { message in
            try store.receive(message)
            if message["method"] as? String == "session.snapshot" {
                XCTAssertEqual(store.chats.first?.title, "TCP chat")
                try channel.send(method: "chat.send", params: ["chat": String(repeating: "ab", count: 16), "text": "TCP send", "requestId": "tcp-1"])
            } else if message["method"] as? String == "session.ack" {
                XCTAssertEqual(store.acks["tcp-1"], true)
                done.fulfill()
            }
        }
        var buffer = Data()
        func read() {
            connection.receive(minimumIncompleteLength: 1, maximumLength: 65536) { data, _, ended, error in
                do {
                    if let data { buffer.append(data) }
                    while let end = buffer.firstIndex(of: 10) {
                        let frameData = buffer.prefix(upTo: end)
                        buffer.removeSubrange(...end)
                        try channel.receive(JSONSerialization.jsonObject(with: frameData) as! [String: Any])
                    }
                    if !ended && error == nil { read() }
                } catch { XCTFail("\(error)"); done.fulfill() }
            }
        }
        connection.stateUpdateHandler = { state in
            if case .ready = state {
                do { try channel.start(); read() }
                catch { XCTFail("\(error)"); done.fulfill() }
            }
        }
        connection.start(queue: network)
        wait(for: [done], timeout: 10)
    }
    func testPythonHandshakeSnapshotAndCommand() throws {
        let root = URL(fileURLWithPath: #filePath).deletingLastPathComponent().deletingLastPathComponent()
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["uv", "run", "python", root.appendingPathComponent("wire_peer.py").path]
        process.currentDirectoryURL = root.deletingLastPathComponent().deletingLastPathComponent().deletingLastPathComponent()
        let input = Pipe(), output = Pipe()
        process.standardInput = input
        process.standardOutput = output
        try process.run()
        defer { try? input.fileHandleForWriting.close(); process.terminate(); process.waitUntilExit() }
        func line() throws -> Data {
            var result = Data()
            while true {
                let byte = try output.fileHandleForReading.read(upToCount: 1) ?? Data()
                if byte.isEmpty { throw RemoteProtocolError.rejected }
                if byte[0] == 10 { return result }
                result.append(byte)
            }
        }
        let bootstrap = try JSONDecoder().decode(RemoteBootstrap.self, from: line())
        let channel = RemoteChannel(bootstrap: bootstrap, identity: .init())
        var store = CompanionStore()
        channel.emit = { frame in
            var data = try JSONSerialization.data(withJSONObject: frame)
            data.append(10)
            try input.fileHandleForWriting.write(contentsOf: data)
        }
        channel.onMessage = { try store.receive($0) }
        func receive() throws {
            try channel.receive(JSONSerialization.jsonObject(with: line()) as! [String: Any])
        }
        try channel.start()
        try receive() // serverHello -> signed clientAuth
        XCTAssertEqual(channel.confirmCode?.count, 6)
        try receive() // signed serverAuth -> encrypted resume
        XCTAssertTrue(channel.established)
        try receive() // encrypted authoritative snapshot
        XCTAssertEqual(store.chats.first?.title, "Wire chat")
        XCTAssertEqual(store.selectedID, String(repeating: "ab", count: 16))
        XCTAssertTrue(store.transcript.contains("Cross-language transcript"))
        try channel.send(method: "chat.send", params: ["chat": String(repeating: "ab", count: 16), "text": "hello", "requestId": "wire-1"])
        try receive()
        try receive()
        try receive()
        XCTAssertEqual(store.acks["wire-1"], true)
        XCTAssertEqual(store.transcript.components(separatedBy: "Wire response").count, 2)
    }
}
