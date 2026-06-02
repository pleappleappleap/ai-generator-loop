package org.soxhlet.pipeline.config;

import jakarta.jms.ConnectionFactory;
import jakarta.jms.XAConnectionFactory;
import org.springframework.beans.factory.annotation.Qualifier;
import org.springframework.context.annotation.Bean;
import org.springframework.context.annotation.Configuration;
import org.springframework.jms.annotation.EnableJms;
import org.springframework.jms.config.DefaultJmsListenerContainerFactory;
import org.springframework.jms.core.JmsTemplate;
import org.springframework.transaction.PlatformTransactionManager;

@EnableJms
@Configuration
public class JmsConfig {

    @Bean
    public DefaultJmsListenerContainerFactory jmsListenerContainerFactory(
            @Qualifier("xaConnectionFactory") XAConnectionFactory xaConnectionFactory,
            PlatformTransactionManager transactionManager) {
        DefaultJmsListenerContainerFactory factory = new DefaultJmsListenerContainerFactory();
        factory.setConnectionFactory((ConnectionFactory) xaConnectionFactory);
        factory.setTransactionManager(transactionManager);
        factory.setSessionTransacted(false);
        factory.setConcurrency("1-1");
        return factory;
    }

    @Bean
    public JmsTemplate jmsTemplate(
            @Qualifier("xaConnectionFactory") XAConnectionFactory xaConnectionFactory) {
        return new JmsTemplate((ConnectionFactory) xaConnectionFactory);
    }
}
