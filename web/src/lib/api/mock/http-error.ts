/** An HTTP error of the mock API: the client turns it into ApiError, as with the real API. */
export class MockHttpError extends Error {
  readonly status: number
  readonly code: string
  constructor(status: number, code: string, message: string) {
    super(message)
    this.status = status
    this.code = code
  }
}
