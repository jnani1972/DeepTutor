import DeepImportCapture from "@/features/video-import/DeepImportCapture";

export default async function DeepImportJobPage({ params }: { params: Promise<{ jobId: string }> }) {
  const { jobId } = await params;
  return <DeepImportCapture jobId={decodeURIComponent(jobId)} />;
}
