import AppKit
import SwiftUI
import ImageIO

/// Small thumbnails/file chips, shared by the draft and saved transcript.
/// ImageIO decodes only a thumbnail while originals remain unchanged on disk.
struct ChatAttachmentStrip: View {
    var attachments: [ChatAttachment]
    var remove: ((String) -> Void)?
    @State private var thumbnails: [String: NSImage] = [:]
    @State private var preview: ChatAttachment?

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .top, spacing: 10) {
                ForEach(attachments) { file in
                    VStack(alignment: .leading, spacing: 4) {
                        Button {
                            if file.kind == "image" { preview = file }
                            else { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: file.path)]) }
                        } label: {
                            if let image = thumbnails[file.id] {
                                Image(nsImage: image).resizable().scaledToFill().frame(width: 78, height: 54)
                                    .clipShape(RoundedRectangle(cornerRadius: 5))
                            } else {
                                Label(file.name, systemImage: "doc.text").font(.system(size: 11))
                                    .foregroundStyle(.secondary).lineLimit(1).frame(maxWidth: 170)
                                    .padding(.vertical, 6)
                            }
                        }.buttonStyle(.plain).help(file.name).accessibilityLabel("Attachment: " + file.name)
                        if file.kind == "image" {
                            Text(file.name).font(.system(size: 10)).foregroundStyle(.secondary)
                                .lineLimit(1).frame(width: 78, alignment: .leading)
                        }
                    }
                    .overlay(alignment: .topTrailing) {
                        if let remove {
                            Button { remove(file.id) } label: {
                                Image(systemName: "xmark.circle.fill").symbolRenderingMode(.palette)
                                    .foregroundStyle(.white, Color.black.opacity(0.75)).font(.system(size: 14))
                            }.buttonStyle(.plain).offset(x: 4, y: -3).accessibilityLabel("Remove " + file.name)
                        }
                    }
                }
            }.padding(.top, 4).padding(.trailing, 4)
        }
        .task(id: attachments.map(\.id)) {
            var images: [String: NSImage] = [:]
            for file in attachments where file.kind == "image" {
                if let existing = thumbnails[file.id] { images[file.id] = existing; continue }
                guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: file.path) as CFURL, nil),
                      let image = CGImageSourceCreateThumbnailAtIndex(source, 0, [
                        kCGImageSourceCreateThumbnailFromImageAlways: true,
                        kCGImageSourceCreateThumbnailWithTransform: true,
                        kCGImageSourceThumbnailMaxPixelSize: 180
                      ] as CFDictionary) else { continue }
                images[file.id] = NSImage(cgImage: image, size: .zero)
            }
            thumbnails = images
        }
        .popover(item: $preview) { file in
            VStack(alignment: .leading, spacing: 8) {
                if let image = NSImage(contentsOfFile: file.path) {
                    Image(nsImage: image).resizable().scaledToFit().frame(maxWidth: 520, maxHeight: 400)
                }
                Text(file.name).font(.system(size: 11)).foregroundStyle(.secondary)
            }.padding(12)
        }
    }
}
