# Evaluate the image's own configuration in the Puma DSL, then replace its bind.
instance_eval(File.read('config/puma.rb'), 'config/puma.rb')
clear_binds!
ssl_bind '0.0.0.0', ENV.fetch('PORT', 3000),
         cert: '/run/tls/server.crt', key: '/run/tls/server.key',
         no_tlsv1: true, no_tlsv1_1: true
