require 'net/http'
require 'openssl'

uri = URI('https://chatwoot:3000/api')
http = Net::HTTP.new(uri.hostname, uri.port, nil)
http.use_ssl = true
http.verify_mode = OpenSSL::SSL::VERIFY_PEER
http.ca_file = '/run/tls/ca.crt'
http.open_timeout = 4
http.read_timeout = 4
exit(http.get(uri.request_uri).code == '200' ? 0 : 1)
