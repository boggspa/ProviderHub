"""Exercise the production JSONL framer/reader with real pipe-sized fragments."""
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


@unittest.skipUnless(sys.platform == "darwin" and shutil.which("xcrun"), "Swift reader test needs macOS")
class ChatEventReaderTests(unittest.TestCase):
    def test_fragmentation_order_and_large_record(self):
        source = Path(__file__).with_name("ChatModel.swift").read_text()
        # Compile the actual transport classes without the AppKit model. This
        # keeps the performance regression independent of SwiftUI and decoding.
        transport = source[source.index("struct ChatLineFramer {"):
                           source.index("/// One top-level field of a worker event")]
        cases = r'''
import Foundation
import Darwin
struct ChatParsedEvent {
    let line: Data
    let event: [String: Any]
    init?(line: Data) {
        guard let event = try? JSONSerialization.jsonObject(with: line) as? [String: Any] else { return nil }
        self.line = line; self.event = event
    }
}
''' + transport + r'''
@main struct Cases {
    static func main() async {
        let unicode = Data("{\"text\":\"👩🏽‍💻é東京\"}\r\n\ninvalid\n{\"event\":\"state\",\"busy\":false}\n".utf8)
        let expected = [Data("{\"text\":\"👩🏽‍💻é東京\"}\r".utf8), Data(), Data("invalid".utf8),
                        Data("{\"event\":\"state\",\"busy\":false}".utf8)]
        for split in 0...unicode.count {
            var framer = ChatLineFramer()
            let lines = framer.lines(appending: unicode.prefix(split)) + framer.lines(appending: unicode.dropFirst(split))
            precondition(lines == expected, "split, CRLF, empty line or nonzero Data index failed")
            precondition(framer.lines(appending: Data()).isEmpty)
            precondition(framer.lines(appending: Data("tail".utf8)).isEmpty, "partial record was emitted")
            precondition(framer.lines(appending: Data([10])) == [Data("tail".utf8)], "tail was lost")
        }
        var byteFramer = ChatLineFramer()
        let byteLines = unicode.flatMap { byteFramer.lines(appending: Data([$0])) }
        precondition(byteLines == expected, "byte-by-byte UTF-8 framing failed")

        // Compare against the captured 56 MB selection snapshot, with 4 KiB
        // reads. The former reader rescanned hundreds of GB for this record.
        let size = 56 * 1024 * 1024
        let large = Data("{\"event\":\"selected\",\"text\":\"".utf8)
            + Data(repeating: 97, count: size) + Data("\"}\n".utf8)
        var framer = ChatLineFramer()
        let began = ProcessInfo.processInfo.systemUptime
        var records = 0
        for offset in stride(from: 0, to: large.count, by: 4096) {
            for line in framer.lines(appending: large.subdata(in: offset..<min(offset + 4096, large.count))) {
                precondition(line.count == large.count - 1 && line.last == 125)
                records += 1
            }
        }
        let seconds = ProcessInfo.processInfo.systemUptime - began
        precondition(records == 1 && seconds < 5, "fragmented framing regressed")
        print("56 MiB framing seconds: \(seconds)")

        // Include invalid/blank records, multiple records in a read, and a
        // Blackboard, roster acknowledgement and turn-ended after the history.
        let selected = Data("{\"event\":\"selected\",\"text\":\"".utf8)
            + Data(repeating: 97, count: 8 * 1024 * 1024) + Data("\"}\n".utf8)
        let controls = Data("\ninvalid\n{\"event\":\"blackboard\"}\n{\"event\":\"team\",\"request\":\"roster\"}\n{\"event\":\"state\",\"busy\":false}\n".utf8)
        let wire = selected + controls
        let reader = ChatEventReader()
        var received: [String] = []
        var mainQueueRan = false
        DispatchQueue.main.async { mainQueueRan = true }
        await withCheckedContinuation { (done: CheckedContinuation<Void, Never>) in
            for offset in stride(from: 0, to: wire.count, by: 4096) {
                reader.consume(wire.subdata(in: offset..<min(offset + 4096, wire.count))) { parsed in
                    precondition(mainQueueRan, "reader blocked the main queue")
                    let event = parsed.event["event"] as! String
                    received.append(event)
                    if event == "team" { precondition(parsed.event["request"] as? String == "roster") }
                    if event == "state" {
                        precondition(parsed.event["busy"] as? Bool == false)
                        precondition(received == ["selected", "blackboard", "team", "state"], "controls lost or reordered")
                        done.resume()
                    }
                }
            }
        }
        print("Fragmented reader and ordered controls passed")
    }
}
'''
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            swift = root / "Cases.swift"
            swift.write_text(cases)
            binary = root / "reader-tests"
            compiled = subprocess.run(["xcrun", "swiftc", "-O", "-parse-as-library", str(swift),
                                       "-o", str(binary)], capture_output=True, text=True, timeout=60)
            self.assertEqual(compiled.returncode, 0, compiled.stderr)
            ran = subprocess.run([str(binary)], capture_output=True, text=True, timeout=15)
            self.assertEqual(ran.returncode, 0, ran.stderr)
            self.assertIn("ordered controls passed", ran.stdout)
            print(ran.stdout.strip())


if __name__ == "__main__":
    unittest.main()
