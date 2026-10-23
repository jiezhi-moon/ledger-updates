import Foundation
import Vision
import ImageIO

struct Token: Codable {
    let text: String
    let x0: Double
    let x1: Double
    let cx: Double
    let cy: Double
    let h: Double
}
struct Result: Codable {
    let ok: Bool
    let engine: String
    let text: String
    let words: [Token]
}

do {
    guard CommandLine.arguments.count >= 2 else {
        throw NSError(domain: "OCR", code: 1, userInfo: [NSLocalizedDescriptionKey: "請提供圖片路徑"])
    }
    let url = URL(fileURLWithPath: CommandLine.arguments[1])
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil), let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
        throw NSError(domain: "OCR", code: 2, userInfo: [NSLocalizedDescriptionKey: "無法讀取圖片"])
    }
    var ranges: [[Double]] = [[0, 1]]
    if CommandLine.arguments.count > 2 {
        ranges = try JSONDecoder().decode([[Double]].self, from: Data(CommandLine.arguments[2].utf8))
    }
    let mode = CommandLine.arguments.count > 3 ? CommandLine.arguments[3] : "text"
    var words: [Token] = []
    var lines: [String] = []
    for range in ranges {
        let y0 = Int((range[0] * Double(image.height)).rounded())
        let y1 = Int((range[1] * Double(image.height)).rounded())
        guard let crop = image.cropping(to: CGRect(x: 0, y: y0, width: image.width, height: y1-y0)) else { continue }
        let request = VNRecognizeTextRequest()
        request.recognitionLevel = .accurate
        let supported = try request.supportedRecognitionLanguages()
        request.recognitionLanguages = (mode == "number" ? ["en-US"] : ["zh-Hans", "zh-Hant", "en-US"]).filter { supported.contains($0) }
        request.usesLanguageCorrection = false
        request.minimumTextHeight = 0.001
        try VNImageRequestHandler(cgImage: crop, options: [:]).perform([request])
        for observation in request.results ?? [] {
            guard let candidate = observation.topCandidates(1).first else { continue }
            let text = candidate.string
            lines.append(text)
            let b = observation.boundingBox
            words.append(Token(text: text, x0: b.minX, x1: b.maxX,
                               cx: b.midX, cy: (Double(y0) + (1-b.midY)*Double(crop.height))/Double(image.height),
                               h: b.height*Double(crop.height)/Double(image.height)))
        }
    }
    let result = Result(ok: true, engine: "Apple Vision", text: lines.joined(separator: "\n"), words: words)
    FileHandle.standardOutput.write(try JSONEncoder().encode(result))
} catch {
    FileHandle.standardError.write(Data(error.localizedDescription.utf8))
    exit(1)
}
