//! Line framing of the helper protocol (ARCHITECTURE Contract 8): UTF-8 JSON, one object per line, at most 64 KiB per line **including**
//! the newline, both directions (`src/arltuner/web/helperlink.py` `MAX_LINE`). Reads are bounded: a longer line is drained in small
//! chunks to its newline and reported as [`Frame::Oversize`]; it is never buffered whole.

use std::io::{self, BufRead, Read};

pub const MAX_LINE: usize = 64 * 1024;
const DRAIN_CHUNK: u64 = 8192;

#[derive(Debug, PartialEq, Eq)]
pub enum Frame {
    /// One line without its newline (a last line cut by EOF is still a line, as in helperlink.py).
    Line(Vec<u8>),
    /// A line over [`MAX_LINE`]: skipped to its newline (or EOF); the stream stays usable.
    Oversize,
    Eof,
}

/// Read one frame. Memory per call is bounded by `max + 1` bytes.
pub fn read_frame<R: BufRead>(r: &mut R, max: usize) -> io::Result<Frame> {
    let mut buf = Vec::new();
    let n = r.by_ref().take(max as u64 + 1).read_until(b'\n', &mut buf)?;
    if n == 0 {
        return Ok(Frame::Eof);
    }
    if buf.last() == Some(&b'\n') {
        if buf.len() > max {
            return Ok(Frame::Oversize);
        }
        buf.pop();
        return Ok(Frame::Line(buf));
    }
    if buf.len() <= max {
        return Ok(Frame::Line(buf)); // EOF in the middle of a short line
    }
    drop(buf);
    let mut chunk = Vec::new();
    loop {
        chunk.clear();
        let got = r.by_ref().take(DRAIN_CHUNK).read_until(b'\n', &mut chunk)?;
        if got == 0 || chunk.last() == Some(&b'\n') {
            return Ok(Frame::Oversize);
        }
    }
}

/// Serialise one request as a compact line with its newline. `None` if it would not fit in [`MAX_LINE`] (or is not strict JSON).
pub fn encode_line(value: &serde_json::Value) -> Option<Vec<u8>> {
    let mut line = serde_json::to_vec(value).ok()?;
    line.push(b'\n');
    (line.len() <= MAX_LINE).then_some(line)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::Cursor;

    fn frames(data: &[u8], max: usize) -> Vec<Frame> {
        let mut c = Cursor::new(data.to_vec());
        let mut out = vec![];
        loop {
            let f = read_frame(&mut c, max).unwrap();
            let done = f == Frame::Eof;
            out.push(f);
            if done {
                return out;
            }
        }
    }

    #[test]
    fn lines_oversize_and_tail() {
        // max 8 including the newline: "1234567\n" fits, "12345678\n" does not
        let f = frames(b"1234567\n12345678\nabc\ntail", 8);
        assert_eq!(f, vec![Frame::Line(b"1234567".to_vec()), Frame::Oversize, Frame::Line(b"abc".to_vec()), Frame::Line(b"tail".to_vec()), Frame::Eof]);
    }

    #[test]
    fn oversize_without_newline_ends_at_eof() {
        assert_eq!(frames(&[b'x'; 100], 8), vec![Frame::Oversize, Frame::Eof]);
    }

    #[test]
    fn oversize_spanning_many_drain_chunks() {
        let mut data = vec![b'x'; 40_000];
        data.extend_from_slice(b"\nok\n");
        assert_eq!(frames(&data, 100), vec![Frame::Oversize, Frame::Line(b"ok".to_vec()), Frame::Eof]);
    }

    #[test]
    fn encode_respects_cap() {
        assert!(encode_line(&serde_json::json!({"a": 1})).is_some());
        let big = serde_json::json!({"a": "x".repeat(MAX_LINE)});
        assert!(encode_line(&big).is_none());
        let edge = "x".repeat(MAX_LINE - 1 - r#"{"a":""}"#.len());
        let fits = serde_json::json!({ "a": edge });
        assert_eq!(encode_line(&fits).unwrap().len(), MAX_LINE);
    }
}
