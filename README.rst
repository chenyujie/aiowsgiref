aiowsgiref
==========
Simple asyncio & wsgiref server

Example
-------

::

  import asyncio
  from aiowsgiref import run

  @asyncio.coroutine
  def demo_app(environ, start_response):
      status = '200 OK' # HTTP Status
      headers = [('Content-type', 'text/plain')]  # HTTP Headers
      start_response(status, headers)
      return [b'Hello World']

  run('127.0.0.1', 8000, demo_app)
