# Test review

Self-review accepted. Validate actual encoded format with ffprobe rather than confusing input -pix_fmt rgba with encoded format. Inspect verbose filter negotiation to exclude an implicit final software scaler. Do not use color equality as a release blocker after explicit operator acceptance. Preserve audio equality and A/V timing as release gates. Performance numbers must be paired, and render CPU must be distinguished from one-time source admission and shared-host wall time.
