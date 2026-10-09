import AppKit
import SwiftUI
import ImageIO

/// Small thumbnails/file chips, shared by the draft and saved transcript.
/// ImageIO decodes only a thumbnail while originals remain unchanged on disk.
/// Thumbnails sit on the row radius with a hairline; other files are chips
/// with a type glyph, name and size, so a draft reads at a glance.
struct ChatAttachmentStrip: View {
    var attachments: [ChatAttachment]
    var remove: ((String) -> Void)?
    @State private var thumbnails: [String: NSImage] = [:]
    @State private var preview: ChatAttachment?
    private typealias Semantic = HubTheme.Semantic

    var body: some View {
        ScrollView(.horizontal, showsIndicators: false) {
            HStack(alignment: .top, spacing: 10) {
                ForEach(attachments) { file in
                    VStack(alignment: .leading, spacing: 4) {
                        Button {
                            if file.kind == "image" { preview = file }
                            else { NSWorkspace.shared.activateFileViewerSelecting([URL(fileURLWithPath: file.path)]) }
                        } label: {
                            if let image = thumbnails[file.id] { thumbnail(image) } else { chip(file) }
                        }.buttonStyle(.plain).help(file.name).accessibilityLabel("Attachment: " + file.name)
                        if file.kind == "image" {
                            Text(file.name).font(.system(size: 10)).foregroundStyle(Semantic.secondaryInk)
                                .lineLimit(1).truncationMode(.middle).frame(width: 78, alignment: .leading).help(file.name)
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

    private func thumbnail(_ image: NSImage) -> some View {
        Image(nsImage: image).resizable().scaledToFill().frame(width: 78, height: 54)
            .clipShape(RoundedRectangle(cornerRadius: HubTheme.Radius.row))
            .overlay(RoundedRectangle(cornerRadius: HubTheme.Radius.row).stroke(Semantic.hairline, lineWidth: 1))
    }

    private func chip(_ file: ChatAttachment) -> some View {
        HStack(spacing: 7) {
            Image(systemName: Self.glyph(for: file.name)).font(.system(size: 12)).foregroundStyle(Semantic.secondaryInk)
            VStack(alignment: .leading, spacing: 1) {
                Text(file.name).font(.system(size: 11, weight: .medium)).foregroundStyle(Semantic.ink)
                    .lineLimit(1).truncationMode(.middle)
                Text(Self.size(file.size)).font(.system(size: 10)).foregroundStyle(Semantic.secondaryInk)
            }
        }
        .padding(.horizontal, 9).padding(.vertical, 5).frame(maxWidth: 190)
        .background(RoundedRectangle(cornerRadius: HubTheme.Radius.row).fill(Semantic.raisedSurface))
        .overlay(RoundedRectangle(cornerRadius: HubTheme.Radius.row).stroke(Semantic.hairline, lineWidth: 1))
    }

    /// A type glyph by extension; anything unrecognised is a plain document.
    static func glyph(for name: String) -> String {
        switch (name as NSString).pathExtension.lowercased() {
        case "pdf": return "doc.richtext"
        case "md", "txt", "rtf", "log": return "doc.text"
        case "csv", "tsv": return "tablecells"
        case "json", "yaml", "yml", "toml", "plist", "xml": return "curlybraces"
        case "swift", "py", "js", "ts", "tsx", "jsx", "rb", "go", "rs", "c", "h", "cpp", "m", "java", "kt", "sh", "zsh", "css", "html":
            return "chevron.left.forwardslash.chevron.right"
        default: return "doc"
        }
    }

    static func size(_ bytes: Int) -> String {
        let formatter = ByteCountFormatter(); formatter.countStyle = .file
        return formatter.string(fromByteCount: Int64(bytes))
    }
}
