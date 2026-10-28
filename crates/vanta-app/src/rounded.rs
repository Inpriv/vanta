//! Rounded-square masking for project icons.
//!
//! Done on the pixels rather than with a clipped `Rectangle`, because the
//! software renderer does not clip images to rounded corners.

/// Corner radius as a share of the icon's shorter side (Modrinth-like).
pub const ICON_ROUNDING: f32 = 0.25;

/// Makes the corners of `img` transparent, with anti-aliased edges.
pub fn round_corners(img: &mut image::RgbaImage, fraction: f32) {
    let (w, h) = img.dimensions();
    let r = w.min(h) as f32 * fraction;
    if r < 1.0 {
        return;
    }
    const SAMPLES: u32 = 4;
    for y in 0..h {
        for x in 0..w {
            // Distance from the pixel to the nearest corner circle's centre,
            // only for pixels inside a corner square.
            let cx = if (x as f32) < r {
                r
            } else if (x as f32) >= w as f32 - r {
                w as f32 - r
            } else {
                continue;
            };
            let cy = if (y as f32) < r {
                r
            } else if (y as f32) >= h as f32 - r {
                h as f32 - r
            } else {
                continue;
            };
            let mut inside = 0;
            for sy in 0..SAMPLES {
                for sx in 0..SAMPLES {
                    let px = x as f32 + (sx as f32 + 0.5) / SAMPLES as f32;
                    let py = y as f32 + (sy as f32 + 0.5) / SAMPLES as f32;
                    if (px - cx).powi(2) + (py - cy).powi(2) <= r * r {
                        inside += 1;
                    }
                }
            }
            let p = img.get_pixel_mut(x, y);
            p[3] = (p[3] as u32 * inside / (SAMPLES * SAMPLES)) as u8;
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn clears_corners_and_keeps_the_middle() {
        let mut img = image::RgbaImage::from_pixel(40, 40, image::Rgba([255, 0, 0, 255]));
        round_corners(&mut img, ICON_ROUNDING);
        assert_eq!(img.get_pixel(0, 0)[3], 0);
        assert_eq!(img.get_pixel(39, 39)[3], 0);
        assert_eq!(img.get_pixel(20, 20)[3], 255);
        assert_eq!(img.get_pixel(20, 0)[3], 255);
    }
}
