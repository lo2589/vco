import Foundation
import ImageIO
import Vision

guard CommandLine.arguments.count >= 2 else {
    fputs("usage: swift vision_ocr.swift IMAGE [fast|accurate]\n", stderr)
    exit(2)
}

let path = CommandLine.arguments[1]
let mode = CommandLine.arguments.count >= 3 ? CommandLine.arguments[2] : "fast"
let customWords = CommandLine.arguments.count >= 4
    ? CommandLine.arguments[3].split(separator: ",").map(String.init)
    : []
guard let source = CGImageSourceCreateWithURL(URL(fileURLWithPath: path) as CFURL, nil),
      let image = CGImageSourceCreateImageAtIndex(source, 0, nil) else {
    fputs("cannot load image\n", stderr)
    exit(2)
}

let request = VNRecognizeTextRequest()
request.recognitionLevel = mode == "accurate" ? .accurate : .fast
request.recognitionLanguages = ["zh-Hans", "en-US"]
request.usesLanguageCorrection = false
request.minimumTextHeight = 0.005
request.customWords = customWords

let started = CFAbsoluteTimeGetCurrent()
try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
let elapsedMs = (CFAbsoluteTimeGetCurrent() - started) * 1000

let width = CGFloat(image.width)
let height = CGFloat(image.height)
let rows: [[String: Any]] = (request.results ?? []).compactMap { observation in
    guard let candidate = observation.topCandidates(1).first else { return nil }
    let box = observation.boundingBox
    let pixelBox: [Double] = [
        Double(box.minX * width),
        Double((1 - box.maxY) * height),
        Double(box.maxX * width),
        Double((1 - box.minY) * height),
    ]
    return [
        "text": candidate.string,
        "confidence": Double(candidate.confidence),
        "bbox": pixelBox,
    ]
}

let output: [String: Any] = [
    "mode": mode,
    "custom_words": customWords,
    "elapsed_ms": elapsedMs,
    "image_size": [image.width, image.height],
    "results": rows,
]
let data = try JSONSerialization.data(withJSONObject: output, options: [.prettyPrinted, .sortedKeys])
FileHandle.standardOutput.write(data)
FileHandle.standardOutput.write(Data("\n".utf8))
