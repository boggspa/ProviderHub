import Foundation
import PDFKit

// Bytes arrive over stdin; this helper never accepts a model-supplied path.
let data = FileHandle.standardInput.readDataToEndOfFile()
guard data.count <= 8 * 1024 * 1024,
      let document = PDFDocument(data: data), !document.isLocked else { exit(1) }
var text = ""
var used = 0
for index in 0..<min(document.pageCount, 100) {
    let page = document.page(at: index)?.string ?? ""
    if used >= 200_000 { break }
    for character in page + "\n" {
        let size = String(character).utf8.count
        if used + size > 200_000 { break }
        text.append(character); used += size
    }
}
FileHandle.standardOutput.write(Data(text.utf8))
